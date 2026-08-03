package com.personalailabs.astraldeep.app.voice

import android.content.Context
import android.media.AudioAttributes
import android.media.AudioFormat
import android.media.AudioTrack as AndroidAudioTrack
import android.util.Log
import com.personalailabs.astraldeep.core.protocol.Inbound
import com.personalailabs.astraldeep.core.protocol.VoiceAnnouncementMedia
import com.personalailabs.astraldeep.core.protocol.VoiceComposerModel
import com.personalailabs.astraldeep.core.protocol.VoiceControlBinding
import com.personalailabs.astraldeep.core.protocol.VoicePlayoutEvent
import com.personalailabs.astraldeep.core.protocol.VoiceSpeechOutcome
import com.personalailabs.astraldeep.core.protocol.VoiceSubmissionRejected
import com.personalailabs.astraldeep.core.protocol.VoiceTranscript
import com.personalailabs.astraldeep.core.protocol.VoiceTurnState
import com.personalailabs.astraldeep.core.protocol.Wire
import io.livekit.android.ConnectOptions
import io.livekit.android.LiveKit
import io.livekit.android.events.RoomEvent
import io.livekit.android.room.Room
import io.livekit.android.room.track.RemoteAudioTrack
import io.livekit.android.room.track.RemoteTrackPublication
import io.livekit.android.room.track.Track
import io.livekit.android.util.LoggingLevel
import java.net.URI
import java.nio.ByteBuffer
import java.time.Instant
import java.util.UUID
import java.util.concurrent.TimeUnit
import kotlin.math.max
import kotlin.math.min
import kotlinx.coroutines.CancellationException
import kotlinx.coroutines.CoroutineScope
import kotlinx.coroutines.Dispatchers
import kotlinx.coroutines.Job
import kotlinx.coroutines.SupervisorJob
import kotlinx.coroutines.cancel
import kotlinx.coroutines.channels.Channel
import kotlinx.coroutines.delay
import kotlinx.coroutines.flow.Flow
import kotlinx.coroutines.flow.MutableSharedFlow
import kotlinx.coroutines.flow.MutableStateFlow
import kotlinx.coroutines.flow.StateFlow
import kotlinx.coroutines.flow.asSharedFlow
import kotlinx.coroutines.flow.asStateFlow
import kotlinx.coroutines.flow.collect
import kotlinx.coroutines.flow.update
import kotlinx.coroutines.launch
import kotlinx.coroutines.sync.Mutex
import kotlinx.coroutines.sync.withLock
import kotlinx.coroutines.withContext
import kotlinx.serialization.json.Json
import kotlinx.serialization.json.JsonNull
import kotlinx.serialization.json.JsonObject
import kotlinx.serialization.json.JsonPrimitive
import kotlinx.serialization.json.booleanOrNull
import kotlinx.serialization.json.buildJsonObject
import kotlinx.serialization.json.contentOrNull
import kotlinx.serialization.json.intOrNull
import kotlinx.serialization.json.put
import kotlinx.serialization.json.putJsonObject
import livekit.org.webrtc.AudioTrackSink
import okhttp3.MediaType.Companion.toMediaType
import okhttp3.OkHttpClient
import okhttp3.Request
import okhttp3.RequestBody.Companion.toRequestBody

const val VOICE_TRANSCRIPT_TOPIC = "astraldeep.voice.transcript.v1"
const val VOICE_ANNOUNCEMENT_TOPIC = "astraldeep.voice.announcement.v1"

/** Bounded class-only failure context; exception messages may contain credentialed URLs. */
internal fun redactedMediaFailureType(error: Throwable): String =
    generateSequence(error) { current -> current.cause }
        .take(4)
        .joinToString(" <- ") { current -> current.javaClass.name.substringAfterLast('.') }

/** Convert a decoded RTC frame into the exact remaining 24-kHz manifest budget. */
internal fun boundedPcmFrameCount(
    remainingSamples24k: Int,
    sampleRateHz: Int,
    availableFrames: Int,
): Int? {
    if (
        remainingSamples24k <= 0 || availableFrames <= 0 ||
        sampleRateHz % 24_000 != 0
    ) {
        return null
    }
    val scale = sampleRateHz / 24_000
    if (scale !in 1..2) return null
    val bounded = min(availableFrames, remainingSamples24k * scale)
    return (bounded - (bounded % scale)).takeIf { it > 0 }
}

internal enum class VoicePublicationDiscovery {
    REJECT,
    REMEMBER,
    ALREADY_REMEMBERED,
}

/** Keep repeated event/snapshot discovery idempotent without weakening worker/audio checks. */
internal fun voicePublicationDiscovery(
    expectedWorkerIdentity: String,
    participantIdentity: String,
    isAudio: Boolean,
    alreadyRemembered: Boolean,
): VoicePublicationDiscovery =
    when {
        participantIdentity != expectedWorkerIdentity || !isAudio -> VoicePublicationDiscovery.REJECT
        alreadyRemembered -> VoicePublicationDiscovery.ALREADY_REMEMBERED
        else -> VoicePublicationDiscovery.REMEMBER
    }

/** Runtime facts checked at the exact user activation gesture. */
data class VoiceMediaCapability(
    val hasMicrophone: Boolean,
    val hasAudioOutput: Boolean,
    val microphonePermission: String,
    val fullDuplex: Boolean,
)

/** Non-secret owner metadata shown only for an explicit takeover decision. */
data class VoiceTakeoverTarget(
    val sessionId: String,
    val deviceKind: String,
    val deviceLabel: String?,
    val generation: Int,
    val mediaGrantRevision: Int,
)

enum class VoiceTerminalNoticeKind {
    REQUEST_DID_NOT_COMPLETE,
    REQUEST_DID_NOT_START,
    TEXT_RESULT_AVAILABLE,
}

/**
 * A terminal voice-turn projection that remains explicit without depending on
 * color or synthesized audio. [serverMessage] is already bounded and
 * content-safe at the wire boundary and is retained verbatim.
 */
data class VoiceTerminalNotice(
    val kind: VoiceTerminalNoticeKind,
    val title: String,
    val serverMessage: String?,
    val guidance: String,
    val turnId: String? = null,
    val occurredAt: Instant? = null,
    val retryPolicy: String? = null,
    val speechUnavailable: Boolean = false,
) {
    val isRequestFailure: Boolean
        get() = kind != VoiceTerminalNoticeKind.TEXT_RESULT_AVAILABLE

    val accessibilityText: String
        get() =
            listOfNotNull(
                title,
                serverMessage?.takeIf(String::isNotBlank),
                guidance,
            ).distinct().joinToString(" ")
}

internal fun terminalNoticeFor(value: VoiceTurnState): VoiceTerminalNotice? =
    when (value.state) {
        "failed", "abandoned" ->
            VoiceTerminalNotice(
                kind = VoiceTerminalNoticeKind.REQUEST_DID_NOT_COMPLETE,
                title = "Voice request didn't complete",
                serverMessage = value.message,
                guidance = "Typed chat is still available.",
                turnId = value.turnId,
                occurredAt = Instant.parse(value.occurredAt),
            )
        "cancelled" ->
            VoiceTerminalNotice(
                kind = VoiceTerminalNoticeKind.REQUEST_DID_NOT_COMPLETE,
                title = "Voice request didn't complete — cancelled",
                serverMessage = value.message,
                guidance = "Typed chat is still available.",
                turnId = value.turnId,
                occurredAt = Instant.parse(value.occurredAt),
            )
        "refused" ->
            VoiceTerminalNotice(
                kind = VoiceTerminalNoticeKind.REQUEST_DID_NOT_START,
                title = "Voice request didn't start",
                serverMessage = value.message,
                guidance = "Typed chat is still available.",
                turnId = value.turnId,
                occurredAt = Instant.parse(value.occurredAt),
            )
        "succeeded" -> {
            val speechUnavailable = value.speechOutcome == VoiceSpeechOutcome.FAILED
            VoiceTerminalNotice(
                kind = VoiceTerminalNoticeKind.TEXT_RESULT_AVAILABLE,
                title =
                    if (speechUnavailable) {
                        "Speech playback failed"
                    } else {
                        "Text result is available"
                    },
                serverMessage = value.message,
                guidance =
                    if (speechUnavailable) {
                        "The request completed. Its committed text result remains available " +
                            "in the conversation."
                    } else {
                        "The text result remains available in the conversation, " +
                            "even if spoken playback is unavailable."
                    },
                turnId = value.turnId,
                occurredAt = Instant.parse(value.occurredAt),
                speechUnavailable = speechUnavailable,
            )
        }
        else -> null
    }

/** A current notice may move only to a distinct turn whose server timestamp is not older. */
internal fun terminalNoticeCanMoveTo(
    current: VoiceTerminalNotice?,
    turnId: String,
    occurredAt: String,
): Boolean {
    if (current == null || current.turnId == turnId) return true
    val currentAt = current.occurredAt ?: return false
    val incomingAt = runCatching { Instant.parse(occurredAt) }.getOrNull() ?: return false
    return !incomingAt.isBefore(currentAt)
}

internal fun reduceTerminalNotice(
    current: VoiceTerminalNotice?,
    value: VoiceTurnState,
): VoiceTerminalNotice? {
    if (!terminalNoticeCanMoveTo(current, value.turnId, value.occurredAt)) return current
    val next = terminalNoticeFor(value) ?: return current?.takeIf { it.turnId == value.turnId }
    if (
        value.state == "succeeded" && value.speechOutcome != VoiceSpeechOutcome.SUPPRESSED &&
        current?.turnId == value.turnId && current.speechUnavailable
    ) {
        return next.copy(
            title = "Speech playback failed",
            guidance =
                "The request completed. Its committed text result remains available " +
                    "in the conversation.",
            speechUnavailable = true,
        )
    }
    return next
}

internal fun terminalNoticeFor(value: VoiceSubmissionRejected): VoiceTerminalNotice =
    VoiceTerminalNotice(
        kind = VoiceTerminalNoticeKind.REQUEST_DID_NOT_START,
        title = "Voice request didn't start",
        serverMessage = value.message,
        guidance =
            if (value.retryPolicy == "explicit_user_retry") {
                "Retry requires a new explicit spoken request. Typed chat is still available."
            } else {
                "This request cannot be retried. Typed chat is still available."
            },
        turnId = value.turnId,
        occurredAt = Instant.parse(value.occurredAt),
        retryPolicy = value.retryPolicy,
    )

internal fun reduceTerminalNotice(
    current: VoiceTerminalNotice?,
    value: VoiceSubmissionRejected,
): VoiceTerminalNotice =
    if (terminalNoticeCanMoveTo(current, value.turnId, value.occurredAt)) {
        terminalNoticeFor(value)
    } else {
        requireNotNull(current)
    }

/** Current UI projection. Controls themselves always come from the server composer model. */
data class VoiceUiState(
    val composer: VoiceComposerModel? = null,
    val phase: String = "off",
    val reason: String = "ready",
    val message: String? = null,
    val terminalNotice: VoiceTerminalNotice? = null,
    val transcriptPreview: String? = null,
    val transcriptFinal: Boolean = false,
    val mediaConnected: Boolean = false,
    val awaitingAcceptance: Int = 0,
    val takeover: VoiceTakeoverTarget? = null,
) {
    val active: Boolean
        get() = phase !in setOf("off", "unavailable", "ended")

    companion object {
        val Unavailable = VoiceUiState(phase = "unavailable", reason = "voice_unavailable")
    }
}

/** Ephemeral UI binding. Its bearer and the user token are redacted from diagnostics. */
class VoiceUiBinding(
    val token: String,
    val deviceId: String,
    val connectionGeneration: String,
    val control: VoiceControlBinding,
    val visibleChatId: String,
) {
    override fun toString(): String =
        "VoiceUiBinding(deviceId=$deviceId, connectionGeneration=$connectionGeneration, " +
            "control=[REDACTED], visibleChatId=$visibleChatId, token=[REDACTED])"
}

data class VoiceRestSession(
    val sessionId: String,
    val deviceId: String,
    val ownerConnectionGeneration: String,
    val visibleChatId: String,
    val appliedVisibleChatId: String?,
    val generation: Int,
    val mediaGrantRevision: Int,
    val chatContextRevision: Int,
    val appliedChatContextRevision: Int?,
    val chatContextSynced: Boolean,
    val state: String,
    val foregroundActive: Boolean,
    val speechMuted: Boolean,
    val microphoneEnabled: Boolean,
)

/** Short-lived no-store LiveKit credential; never persisted or included in toString(). */
class LiveKitVoiceGrant(
    val grantId: String,
    val sessionId: String,
    val generation: Int,
    val mediaGrantRevision: Int,
    val expiresAt: String,
    val url: String,
    val joinToken: String,
    val roomName: String,
    val participantIdentity: String,
    val workerIdentity: String,
) {
    override fun toString(): String =
        "LiveKitVoiceGrant(grantId=$grantId, sessionId=$sessionId, generation=$generation, " +
            "mediaGrantRevision=$mediaGrantRevision, expiresAt=$expiresAt, url=[REDACTED], " +
            "joinToken=[REDACTED], roomName=$roomName, participantIdentity=$participantIdentity, " +
            "workerIdentity=$workerIdentity)"
}

sealed interface VoiceStartOutcome {
    data class Started(val session: VoiceRestSession, val grant: LiveKitVoiceGrant) : VoiceStartOutcome

    data class TakeoverRequired(val target: VoiceTakeoverTarget, val message: String?) : VoiceStartOutcome

    data class Failed(val reason: String, val message: String? = null) : VoiceStartOutcome
}

interface VoiceControlApi {
    suspend fun start(
        binding: VoiceUiBinding,
        activationId: String,
        capability: VoiceMediaCapability,
    ): VoiceStartOutcome

    suspend fun takeover(
        binding: VoiceUiBinding,
        activationId: String,
        target: VoiceTakeoverTarget,
        capability: VoiceMediaCapability,
    ): VoiceStartOutcome

    suspend fun update(
        binding: VoiceUiBinding,
        session: VoiceRestSession,
        fields: JsonObject,
    ): Result<VoiceRestSession>

    suspend fun stopSpeech(binding: VoiceUiBinding, session: VoiceRestSession): Boolean

    suspend fun end(binding: VoiceUiBinding, session: VoiceRestSession): Boolean
}

sealed interface VoiceMediaEvent {
    data object Connected : VoiceMediaEvent

    data object Reconnecting : VoiceMediaEvent

    data class Data(val topic: String?, val participantIdentity: String?, val payload: ByteArray) : VoiceMediaEvent

    data class Disconnected(val unexpected: Boolean) : VoiceMediaEvent

    /** One phase derived from the locally matched PCM render pipeline. */
    data class Playout(
        val announcementId: String,
        val announcementSequence: Int,
        val phase: String,
    ) : VoiceMediaEvent

    /** A manifest/track pair failed before any local sample rendered. */
    data class AnnouncementDropped(
        val announcementId: String,
        val announcementSequence: Int,
    ) : VoiceMediaEvent

    data object Failed : VoiceMediaEvent
}

interface VoiceMediaClient {
    val events: Flow<VoiceMediaEvent>

    suspend fun connect(grant: LiveKitVoiceGrant)

    suspend fun setMicrophoneEnabled(enabled: Boolean)

    /** Queue one already-validated manifest for exact worker-track matching. */
    suspend fun queueAnnouncement(value: VoiceAnnouncementMedia): Boolean

    /** Stop local speech immediately; accepted agent work is unaffected. */
    fun interruptPlayout()

    fun disconnect()
}

/**
 * Official direct-RTC LiveKit Android adapter. It publishes only the microphone;
 * there is intentionally no data-publication method on this client surface.
 */
class LiveKitVoiceMediaClient(
    context: Context,
    private val scope: CoroutineScope,
) : VoiceMediaClient {
    private data class PublishedTrack(
        val publication: RemoteTrackPublication,
        val participantIdentity: String,
        var expiryJob: Job? = null,
    )

    private data class PcmChunk(
        val bytes: ByteArray,
        val bitsPerSample: Int,
        val sampleRateHz: Int,
        val channelCount: Int,
        val frameCount: Int,
    )

    private data class ActivePlayout(
        val manifest: VoiceAnnouncementMedia,
        val publication: RemoteTrackPublication,
        val channel: Channel<PcmChunk> = Channel(PCM_CHANNEL_CAPACITY),
        var track: RemoteAudioTrack? = null,
        var sink: AudioTrackSink? = null,
        var renderJob: Job? = null,
        var started: Boolean = false,
    )

    private val applicationContext = context.applicationContext
    private val _events = MutableSharedFlow<VoiceMediaEvent>(extraBufferCapacity = 32)
    override val events: Flow<VoiceMediaEvent> = _events.asSharedFlow()

    private var room: Room? = null
    private var eventJob: Job? = null
    private var intentionalDisconnect = false
    private var currentGrant: LiveKitVoiceGrant? = null
    private val publishedTracks = linkedMapOf<String, PublishedTrack>()
    private val announcementQueue = ArrayDeque<VoiceAnnouncementMedia>()
    private val acceptedAnnouncementIds = mutableSetOf<String>()
    private var activePlayout: ActivePlayout? = null

    override suspend fun connect(grant: LiveKitVoiceGrant) {
        disconnect()
        currentGrant = grant
        // The SDK's INFO diagnostics can include credentialed signaling/SDP.
        // AstralDeep reports only its own bounded, redacted media failures.
        LiveKit.loggingLevel = LoggingLevel.OFF
        LiveKit.enableWebRTCLogging = false
        val next = LiveKit.create(applicationContext)
        room = next
        eventJob =
            scope.launch {
                next.events.events.collect { event ->
                    when (event) {
                        is RoomEvent.Connected, is RoomEvent.Reconnected -> _events.emit(VoiceMediaEvent.Connected)
                        is RoomEvent.Reconnecting -> _events.emit(VoiceMediaEvent.Reconnecting)
                        is RoomEvent.DataReceived ->
                            _events.emit(
                                VoiceMediaEvent.Data(
                                    topic = event.topic,
                                    participantIdentity = event.participant?.identity?.value,
                                    payload = event.data.copyOf(),
                                ),
                            )
                        is RoomEvent.TrackPublished -> trackPublished(event)
                        is RoomEvent.TrackSubscribed -> trackSubscribed(event)
                        is RoomEvent.TrackUnpublished -> trackRemoved(event.publication.sid)
                        is RoomEvent.TrackUnsubscribed -> trackRemoved(event.publications.sid)
                        is RoomEvent.Disconnected ->
                            _events.emit(VoiceMediaEvent.Disconnected(unexpected = !intentionalDisconnect))
                        is RoomEvent.FailedToConnect -> _events.emit(VoiceMediaEvent.Failed)
                        else -> Unit
                    }
                }
            }
        try {
            next.connect(
                grant.url,
                grant.joinToken,
                // Assistant tracks are subscribed only after their strict
                // manifest is matched; this prevents audio-before-manifest.
                ConnectOptions(autoSubscribe = false, audio = true, video = false),
            )
            // A publication included in the join snapshot can predate the
            // subscriber transport and therefore have no observable
            // TrackPublished event. connect() returning is the initial
            // reconciliation seam documented by the media contract.
            reconcileExistingPublications(next)
            _events.emit(VoiceMediaEvent.Connected)
        } catch (error: Exception) {
            Log.e(LOG_TAG, "LiveKit connection failed (${redactedMediaFailureType(error)})")
            disconnect()
            _events.emit(VoiceMediaEvent.Failed)
            throw error
        }
    }

    override suspend fun setMicrophoneEnabled(enabled: Boolean) {
        room?.localParticipant?.setMicrophoneEnabled(enabled)
    }

    override suspend fun queueAnnouncement(value: VoiceAnnouncementMedia): Boolean {
        val grant = currentGrant ?: return false
        val trackSid = value.trackSid ?: return false
        if (
            value.transport != "livekit" || value.workerIdentity != grant.workerIdentity ||
            value.sessionId != grant.sessionId || value.generation != grant.generation ||
            value.mediaGrantRevision != grant.mediaGrantRevision || value.sampleRateHz != OUTPUT_SAMPLE_RATE_HZ ||
            value.durationSamples !in 1..MAX_QUANTUM_SAMPLES ||
            trackSid.isBlank() || value.trackName.isNullOrBlank() ||
            acceptedAnnouncementIds.contains(value.announcementId) ||
            announcementQueue.size >= MAX_PENDING_ANNOUNCEMENTS
        ) {
            return false
        }
        acceptedAnnouncementIds += value.announcementId
        announcementQueue.addLast(value)
        scope.launch {
            delay(MANIFEST_MATCH_TIMEOUT_MILLIS)
            expireUnmatchedAnnouncement(value.announcementId)
        }
        // Re-scan on manifest arrival as a second bounded reconciliation seam.
        // This remains fail closed: every discovered publication is left
        // unsubscribed until its exact content-free manifest matches.
        room?.let(::reconcileExistingPublications) ?: reconcilePlayout()
        return true
    }

    override fun interruptPlayout() {
        val dropped = announcementQueue.toList()
        announcementQueue.clear()
        dropped.forEach { manifest ->
            acceptedAnnouncementIds.remove(manifest.announcementId)
            _events.tryEmit(
                VoiceMediaEvent.AnnouncementDropped(
                    manifest.announcementId,
                    manifest.announcementSequence,
                ),
            )
        }
        stopActivePlayout(interrupted = true)
    }

    override fun disconnect() {
        intentionalDisconnect = true
        interruptPlayout()
        acceptedAnnouncementIds.clear()
        publishedTracks.values.forEach { published ->
            published.expiryJob?.cancel()
            published.publication.setSubscribed(false)
        }
        publishedTracks.clear()
        currentGrant = null
        eventJob?.cancel()
        eventJob = null
        room?.localParticipant?.let { participant ->
            scope.launch { runCatching { participant.setMicrophoneEnabled(false) } }
        }
        room?.disconnect()
        room?.release()
        room = null
        intentionalDisconnect = false
    }

    private fun trackPublished(event: RoomEvent.TrackPublished) {
        val publication = event.publication as? RemoteTrackPublication ?: return
        val participantIdentity = event.participant.identity?.value ?: return
        rememberPublishedTrack(publication, participantIdentity)
        reconcilePlayout()
    }

    private fun reconcileExistingPublications(activeRoom: Room) {
        activeRoom.remoteParticipants.values.forEach { participant ->
            val participantIdentity = participant.identity?.value ?: return@forEach
            participant.trackPublications.values.forEach publications@{ candidate ->
                val publication = candidate as? RemoteTrackPublication ?: return@publications
                rememberPublishedTrack(publication, participantIdentity)
            }
        }
        reconcilePlayout()
    }

    private fun rememberPublishedTrack(
        publication: RemoteTrackPublication,
        participantIdentity: String,
    ) {
        val grant = currentGrant ?: return
        when (
            voicePublicationDiscovery(
                expectedWorkerIdentity = grant.workerIdentity,
                participantIdentity = participantIdentity,
                isAudio = publication.kind == Track.Kind.AUDIO,
                alreadyRemembered = publishedTracks.containsKey(publication.sid),
            )
        ) {
            VoicePublicationDiscovery.REJECT -> {
                publication.setSubscribed(false)
                return
            }
            // Event delivery and snapshot reconciliation may discover the same
            // object. Do not mutate it here: it may already be the subscribed,
            // manifest-matched active playout.
            VoicePublicationDiscovery.ALREADY_REMEMBERED -> return
            VoicePublicationDiscovery.REMEMBER -> Unit
        }
        publication.setSubscribed(false)
        val published = PublishedTrack(publication, participantIdentity)
        publishedTracks[publication.sid] = published
        published.expiryJob =
            scope.launch {
                delay(MANIFEST_MATCH_TIMEOUT_MILLIS)
                expireUnmatchedTrack(publication.sid)
            }
    }

    private fun trackSubscribed(event: RoomEvent.TrackSubscribed) {
        val active = activePlayout
        val publication = event.publication as? RemoteTrackPublication
        val audioTrack = event.track as? RemoteAudioTrack
        if (
            active == null || publication == null || audioTrack == null ||
            publication.sid != active.manifest.trackSid || publication.name != active.manifest.trackName ||
            event.participant.identity?.value != active.manifest.workerIdentity
        ) {
            publication?.setSubscribed(false)
            return
        }
        if (active.track != null) return
        // Mute LiveKit's automatic output before attaching the bounded PCM
        // sink. AndroidAudioTrack below is the only render path.
        audioTrack.setVolume(0.0)
        val sink =
            AudioTrackSink { audioData, bitsPerSample, sampleRate, channels, frames, _ ->
                receivePcm(active.manifest.announcementId, audioData, bitsPerSample, sampleRate, channels, frames)
            }
        active.track = audioTrack
        active.sink = sink
        audioTrack.addSink(sink)
        active.renderJob = scope.launch(Dispatchers.IO) { renderPcm(active) }
    }

    private fun trackRemoved(trackSid: String) {
        publishedTracks.remove(trackSid)?.expiryJob?.cancel()
        val active = activePlayout
        if (active != null && active.manifest.trackSid == trackSid) {
            if (active.started) active.channel.close() else failActivePlayout(active.manifest.announcementId)
        }
    }

    private fun reconcilePlayout() {
        if (activePlayout != null) return
        val manifest = announcementQueue.firstOrNull() ?: return
        val trackSid = manifest.trackSid ?: return
        val published = publishedTracks[trackSid] ?: return
        if (
            published.participantIdentity != manifest.workerIdentity ||
            published.publication.name != manifest.trackName
        ) {
            announcementQueue.removeFirst()
            acceptedAnnouncementIds.remove(manifest.announcementId)
            published.publication.setSubscribed(false)
            publishedTracks.remove(trackSid)
            _events.tryEmit(
                VoiceMediaEvent.AnnouncementDropped(
                    manifest.announcementId,
                    manifest.announcementSequence,
                ),
            )
            return reconcilePlayout()
        }
        announcementQueue.removeFirst()
        published.expiryJob?.cancel()
        activePlayout = ActivePlayout(manifest, published.publication)
        published.publication.setSubscribed(true)
    }

    private fun expireUnmatchedAnnouncement(announcementId: String) {
        val manifest = announcementQueue.firstOrNull { it.announcementId == announcementId } ?: return
        announcementQueue.remove(manifest)
        acceptedAnnouncementIds.remove(announcementId)
        manifest.trackSid?.let { sid ->
            publishedTracks.remove(sid)?.let { published ->
                published.expiryJob?.cancel()
                published.publication.setSubscribed(false)
            }
        }
        _events.tryEmit(
            VoiceMediaEvent.AnnouncementDropped(
                manifest.announcementId,
                manifest.announcementSequence,
            ),
        )
        reconcilePlayout()
    }

    private fun expireUnmatchedTrack(trackSid: String) {
        val active = activePlayout
        if (active?.manifest?.trackSid == trackSid || announcementQueue.any { it.trackSid == trackSid }) return
        publishedTracks.remove(trackSid)?.publication?.setSubscribed(false)
    }

    private fun receivePcm(
        announcementId: String,
        audioData: ByteBuffer,
        bitsPerSample: Int,
        sampleRateHz: Int,
        channelCount: Int,
        frameCount: Int,
    ) {
        val active = activePlayout
        if (active?.manifest?.announcementId != announcementId) return
        val bytesPerFrame = (bitsPerSample / 8) * channelCount
        val byteCount = frameCount * bytesPerFrame
        if (
            bitsPerSample != PCM_BITS_PER_SAMPLE || sampleRateHz !in ACCEPTED_WEBRTC_SAMPLE_RATES ||
            channelCount !in 1..2 || frameCount <= 0 || bytesPerFrame <= 0 ||
            byteCount <= 0 || audioData.remaining() < byteCount
        ) {
            scope.launch { failActivePlayout(announcementId) }
            return
        }
        val duplicate = audioData.duplicate()
        val bytes = ByteArray(byteCount)
        duplicate.get(bytes)
        if (
            active.channel.trySend(
                PcmChunk(bytes, bitsPerSample, sampleRateHz, channelCount, frameCount),
            ).isFailure
        ) {
            scope.launch { failActivePlayout(announcementId) }
        }
    }

    private suspend fun renderPcm(active: ActivePlayout) {
        var output: AndroidAudioTrack? = null
        var outputRate = 0
        var outputChannels = 0
        var remainingSamples = active.manifest.durationSamples
        var writtenFrames = 0L
        try {
            for (chunk in active.channel) {
                val rateScale = chunk.sampleRateHz / OUTPUT_SAMPLE_RATE_HZ
                if (
                    chunk.bitsPerSample != PCM_BITS_PER_SAMPLE ||
                    chunk.sampleRateHz % OUTPUT_SAMPLE_RATE_HZ != 0 || rateScale !in 1..2
                ) {
                    throw IllegalArgumentException("unsupported PCM profile")
                }
                if (output == null) {
                    output = createAudioOutput(chunk.sampleRateHz, chunk.channelCount)
                    outputRate = chunk.sampleRateHz
                    outputChannels = chunk.channelCount
                    output.play()
                } else if (outputRate != chunk.sampleRateHz || outputChannels != chunk.channelCount) {
                    throw IllegalArgumentException("PCM profile changed")
                }
                val allowedFrames =
                    boundedPcmFrameCount(remainingSamples, chunk.sampleRateHz, chunk.frameCount)
                        ?: throw IllegalArgumentException("invalid PCM duration")
                val byteCount = allowedFrames * chunk.channelCount * PCM_BYTES_PER_SAMPLE
                var offset = 0
                while (offset < byteCount) {
                    val written = output.write(chunk.bytes, offset, byteCount - offset, AndroidAudioTrack.WRITE_BLOCKING)
                    if (written <= 0) throw IllegalStateException("audio output failed")
                    offset += written
                }
                val normalizedFrames = allowedFrames / rateScale
                remainingSamples -= normalizedFrames
                writtenFrames += allowedFrames.toLong()
                if (!active.started) {
                    active.started = true
                    _events.emit(
                        VoiceMediaEvent.Playout(
                            active.manifest.announcementId,
                            active.manifest.announcementSequence,
                            "started",
                        ),
                    )
                }
                if (remainingSamples == 0) {
                    awaitRendered(output, writtenFrames, outputRate)
                    finishActivePlayout(active.manifest.announcementId)
                    return
                }
            }
            if (remainingSamples > 0) failActivePlayout(active.manifest.announcementId)
        } catch (_: Exception) {
            failActivePlayout(active.manifest.announcementId)
        } finally {
            runCatching { output?.pause() }
            runCatching { output?.flush() }
            runCatching { output?.release() }
        }
    }

    private fun createAudioOutput(
        sampleRateHz: Int,
        channelCount: Int,
    ): AndroidAudioTrack {
        val channelMask =
            if (channelCount == 1) AudioFormat.CHANNEL_OUT_MONO else AudioFormat.CHANNEL_OUT_STEREO
        val minimum = AndroidAudioTrack.getMinBufferSize(sampleRateHz, channelMask, AudioFormat.ENCODING_PCM_16BIT)
        check(minimum > 0)
        val output =
            AndroidAudioTrack.Builder()
                .setAudioAttributes(
                    AudioAttributes.Builder()
                        .setUsage(AudioAttributes.USAGE_ASSISTANT)
                        .setContentType(AudioAttributes.CONTENT_TYPE_SPEECH)
                        .build(),
                ).setAudioFormat(
                    AudioFormat.Builder()
                        .setEncoding(AudioFormat.ENCODING_PCM_16BIT)
                        .setSampleRate(sampleRateHz)
                        .setChannelMask(channelMask)
                        .build(),
                ).setTransferMode(AndroidAudioTrack.MODE_STREAM)
                .setBufferSizeInBytes(max(minimum, sampleRateHz * channelCount * PCM_BYTES_PER_SAMPLE / 5))
                .build()
        check(output.state == AndroidAudioTrack.STATE_INITIALIZED)
        return output
    }

    private suspend fun awaitRendered(
        output: AndroidAudioTrack,
        expectedFrames: Long,
        sampleRateHz: Int,
    ) {
        val deadlineNanos = System.nanoTime() + ((expectedFrames * 1_000_000_000L) / sampleRateHz) + 500_000_000L
        while (output.playbackHeadPosition.toLong() < expectedFrames && System.nanoTime() < deadlineNanos) {
            delay(5)
        }
        check(output.playbackHeadPosition.toLong() >= expectedFrames)
    }

    private fun finishActivePlayout(announcementId: String) {
        val active = activePlayout?.takeIf { it.manifest.announcementId == announcementId } ?: return
        cleanupActive(active)
        _events.tryEmit(
            VoiceMediaEvent.Playout(
                active.manifest.announcementId,
                active.manifest.announcementSequence,
                "finished",
            ),
        )
        reconcilePlayout()
    }

    private fun failActivePlayout(announcementId: String) {
        val active = activePlayout?.takeIf { it.manifest.announcementId == announcementId } ?: return
        if (active.started) {
            stopActivePlayout(interrupted = true)
        } else {
            cleanupActive(active)
            _events.tryEmit(
                VoiceMediaEvent.AnnouncementDropped(
                    active.manifest.announcementId,
                    active.manifest.announcementSequence,
                ),
            )
        }
        reconcilePlayout()
    }

    private fun stopActivePlayout(interrupted: Boolean) {
        val active = activePlayout ?: return
        cleanupActive(active)
        if (interrupted && active.started) {
            _events.tryEmit(
                VoiceMediaEvent.Playout(
                    active.manifest.announcementId,
                    active.manifest.announcementSequence,
                    "interrupted",
                ),
            )
        } else if (interrupted) {
            _events.tryEmit(
                VoiceMediaEvent.AnnouncementDropped(
                    active.manifest.announcementId,
                    active.manifest.announcementSequence,
                ),
            )
        }
        reconcilePlayout()
    }

    private fun cleanupActive(active: ActivePlayout) {
        active.channel.close()
        active.renderJob?.cancel()
        active.sink?.let { sink -> active.track?.removeSink(sink) }
        active.track?.setVolume(0.0)
        active.publication.setSubscribed(false)
        publishedTracks.remove(active.publication.sid)?.expiryJob?.cancel()
        acceptedAnnouncementIds.remove(active.manifest.announcementId)
        if (activePlayout === active) activePlayout = null
    }

    companion object {
        private const val LOG_TAG = "AstralVoice"
        private const val OUTPUT_SAMPLE_RATE_HZ = 24_000
        private const val MAX_QUANTUM_SAMPLES = 96_000
        private const val MAX_PENDING_ANNOUNCEMENTS = 8
        private const val MANIFEST_MATCH_TIMEOUT_MILLIS = 1_000L
        private const val PCM_BITS_PER_SAMPLE = 16
        private const val PCM_BYTES_PER_SAMPLE = 2
        private const val PCM_CHANNEL_CAPACITY = 512
        private val ACCEPTED_WEBRTC_SAMPLE_RATES = setOf(24_000, 48_000)
    }
}

/**
 * Serialized owner of one Android voice session. Audio and bearer material stay
 * memory-only. Final transcripts are bounded and retried with immutable IDs
 * until an exact normal-chat acknowledgement or rejection arrives.
 */
class VoiceSessionController(
    private val api: VoiceControlApi,
    private val media: VoiceMediaClient,
    private val scope: CoroutineScope = CoroutineScope(SupervisorJob() + Dispatchers.Main.immediate),
    private val uuidFactory: () -> String = { UUID.randomUUID().toString() },
    private val clock: () -> Instant = Instant::now,
    private val retryDelayMillis: Long = 2_500L,
) {
    private data class UiConnection(
        val token: String,
        val deviceId: String,
        val connectionGeneration: String,
        val visibleChatId: String?,
    )

    private data class PendingFinal(
        val transcript: VoiceTranscript,
        val byteLength: Int,
    )

    private data class PendingAnnouncement(
        val manifest: VoiceAnnouncementMedia,
        var phase: String? = null,
    )

    private data class ResultBudget(
        val quantumIndex: Int,
        val reservedSamples: Int,
        val locallyDeclaredSamples: Int,
    )

    private data class SessionFence(
        val lifecycleToken: Any,
        val connectionGeneration: String?,
        val sessionId: String,
        val generation: Int,
        val mediaGrantRevision: Int,
    )

    private val _state = MutableStateFlow(VoiceUiState())
    val state: StateFlow<VoiceUiState> = _state.asStateFlow()

    private var connection: UiConnection? = null
    private var pendingControl: VoiceControlBinding? = null
    private var binding: VoiceUiBinding? = null
    private var session: VoiceRestSession? = null
    private var grant: LiveKitVoiceGrant? = null
    private var composerRevision = -1
    private val transcriptSequence = mutableMapOf<String, Int>()
    private val announcementSequence = mutableMapOf<String, Int>()
    private val pendingAnnouncements = linkedMapOf<String, PendingAnnouncement>()
    private val resultBudgets = mutableMapOf<String, ResultBudget>()
    private val pendingFinals = linkedMapOf<String, PendingFinal>()
    private val pendingJobs = mutableMapOf<String, Job>()
    private var submitter: ((VoiceTranscript, String) -> Boolean)? = null
    private var playoutReporter: ((VoicePlayoutEvent) -> Boolean)? = null
    private var playoutClientSequence = 0
    private var observedTurnId: String? = null
    private var appForeground = true
    private var leaseRenewalJob: Job? = null
    private var leaseRenewalInFlight: Any? = null
    private var lifecycleToken: Any = Any()
    private val sessionUpdateMutex = Mutex()
    private val mediaTransitionMutex = Mutex()

    init {
        scope.launch { media.events.collect(::consumeMediaEvent) }
    }

    fun setTranscriptSubmitter(value: (VoiceTranscript, String) -> Boolean) {
        submitter = value
    }

    fun setPlayoutReporter(value: (VoicePlayoutEvent) -> Boolean) {
        playoutReporter = value
    }

    /** A new socket generation invalidates the old short-lived binding immediately. */
    fun installUiConnection(
        token: String,
        deviceId: String,
        connectionGeneration: String,
        visibleChatId: String?,
    ) {
        if (!isUuid4(deviceId) || !isUuid4(connectionGeneration)) return
        val changed = connection?.connectionGeneration != connectionGeneration
        connection = UiConnection(token, deviceId, connectionGeneration, visibleChatId)
        if (changed) {
            invalidateLifecycle()
            stopLeaseRenewal()
            pendingControl = null
            binding = null
            composerRevision = -1
            media.disconnect()
            _state.update { current ->
                if (session == null) {
                    if (current.staleWithoutSession()) {
                        current.copy(
                            composer = null,
                            phase = "off",
                            reason = "ready",
                            message = null,
                            terminalNotice = null,
                            takeover = null,
                            mediaConnected = false,
                        )
                    } else {
                        current.copy(
                            composer = null,
                            terminalNotice = null,
                            takeover = null,
                            mediaConnected = false,
                        )
                    }
                } else {
                    current.copy(
                        composer = null,
                        phase = "reconnecting",
                        reason = "network_interrupted",
                        message = messageFor("reconnecting", "network_interrupted"),
                        terminalNotice = null,
                        takeover = null,
                        mediaConnected = false,
                    )
                }
            }
        }
    }

    fun updateVisibleChatLocally(chatId: String?) {
        val current = connection ?: return
        connection = current.copy(visibleChatId = chatId)
        if (chatId == null || !isUuid4(chatId)) {
            val activeSession = session
            if (activeSession == null) {
                binding = null
                restoreServerProjectionWithoutSession()
            } else {
                val fence = sessionFence(activeSession)
                media.interruptPlayout()
                feedback(
                    "connecting",
                    "chat_context_unavailable",
                    "Waiting for the voice chat context…",
                )
                scope.launch {
                    if (sessionMatches(fence)) runCatching { media.setMicrophoneEnabled(false) }
                }
            }
            return
        }
        pendingControl?.let { control ->
            if (control.connectionGeneration == current.connectionGeneration && control.deviceId == current.deviceId) {
                binding = VoiceUiBinding(current.token, current.deviceId, current.connectionGeneration, control, chatId)
            }
        }
        val activeSession = session ?: return
        val currentBinding = binding ?: return
        val fence = sessionFence(activeSession)
        scope.launch {
            if (!sessionMatches(fence) || connection?.visibleChatId != chatId) return@launch
            runCatching { media.setMicrophoneEnabled(false) }
            val result =
                sessionUpdateMutex.withLock {
                    if (!sessionMatches(fence) || connection?.visibleChatId != chatId) return@withLock null
                    api.update(
                        currentBinding,
                        activeSession,
                        buildJsonObject { put("visible_chat_id", chatId) },
                    )
                } ?: return@launch
            if (!sessionMatches(fence) || connection?.visibleChatId != chatId) return@launch
            result.onSuccess { updated ->
                if (!sameLease(activeSession, updated)) return@onSuccess
                session = updated
                if (
                    sessionMatches(fence) && connection?.visibleChatId == chatId &&
                    updated.chatContextSynced && updated.microphoneEnabled
                ) {
                    runCatching { media.setMicrophoneEnabled(true) }
                }
            }.onFailure {
                if (sessionMatches(fence) && connection?.visibleChatId == chatId) {
                    feedback("error", "chat_context_unavailable")
                }
            }
        }
    }

    fun consumeBinding(value: VoiceControlBinding) {
        val current = connection ?: return
        if (
            value.deviceId != current.deviceId ||
            value.connectionGeneration != current.connectionGeneration ||
            value.binding.length !in 32..512 ||
            !BINDING_VALUE.matches(value.binding) ||
            parseFuture(value.expiresAt) == null
        ) {
            return
        }
        pendingControl = value
        val chatId = current.visibleChatId
        if (chatId != null) {
            binding = VoiceUiBinding(current.token, current.deviceId, current.connectionGeneration, value, chatId)
        }
        val retainedFinals = pendingFinals.values.toList()
        scope.launch { retainedFinals.forEach { sendPending(it) } }
    }

    fun awaitingChat() {
        feedback("connecting", "ready", "Creating a conversation for voice…")
    }

    fun activationFailed(
        reason: String,
        message: String? = null,
    ) {
        feedback("error", reason, message)
    }

    fun consumeComposer(frame: Inbound.ComposerState) {
        val current = connection ?: return
        if (frame.connectionGeneration != current.connectionGeneration || frame.revision <= composerRevision) return
        composerRevision = frame.revision
        val composer = frame.voice
        val takeoverSessionId = composer.sessionId
        val takeoverGeneration = composer.generation
        val takeoverGrantRevision = composer.mediaGrantRevision
        _state.update { previous ->
            previous.copy(
                composer = composer,
                phase = composer.state,
                reason = composer.reason,
                message = composer.message ?: messageFor(composer.state, composer.reason),
                terminalNotice =
                    previous.terminalNotice.takeUnless {
                        composer.state in setOf("off", "ended")
                    },
                takeover =
                    if (composer.reason == "takeover_required" && takeoverSessionId != null &&
                        takeoverGeneration != null && takeoverGrantRevision != null
                    ) {
                        VoiceTakeoverTarget(
                            takeoverSessionId,
                            composer.ownerDevice?.deviceKind ?: "unknown",
                            composer.ownerDevice?.deviceLabel,
                            takeoverGeneration,
                            takeoverGrantRevision,
                        )
                    } else {
                        previous.takeover?.takeIf { composer.reason == "takeover_required" }
                    },
            )
        }
        if (composer.state == "ended") clearMediaSession(retainPending = true)
    }

    suspend fun activate(capability: VoiceMediaCapability) {
        activateInternal(capability, takeover = null)
    }

    suspend fun takeOver(capability: VoiceMediaCapability) {
        val target = _state.value.takeover
        if (target == null) {
            feedback("error", "stale_generation")
            return
        }
        activateInternal(capability, takeover = target)
    }

    private suspend fun activateInternal(
        capability: VoiceMediaCapability,
        takeover: VoiceTakeoverTarget?,
    ) {
        val currentBinding = currentBindingOrFeedback() ?: return
        val activationToken = lifecycleToken
        val capabilityFailure = capabilityFailure(capability)
        if (capabilityFailure != null) {
            feedback("error", capabilityFailure)
            return
        }
        _state.update { it.copy(terminalNotice = null) }
        feedback("connecting", "ready")
        val activationId = uuidFactory().takeIf(::isUuid4)
        if (activationId == null) {
            feedback("error", "internal_error")
            return
        }
        val outcome =
            if (takeover == null) {
                api.start(currentBinding, activationId, capability)
            } else {
                api.takeover(currentBinding, activationId, takeover, capability)
            }
        if (lifecycleToken !== activationToken || binding !== currentBinding) {
            if (outcome is VoiceStartOutcome.Started) {
                runCatching { api.end(currentBinding, outcome.session) }
            }
            return
        }
        when (outcome) {
            is VoiceStartOutcome.TakeoverRequired -> {
                media.disconnect()
                _state.update {
                    it.copy(
                        phase = "off",
                        reason = "takeover_required",
                        message = outcome.message ?: messageFor("off", "takeover_required"),
                        mediaConnected = false,
                        takeover = outcome.target,
                    )
                }
            }
            is VoiceStartOutcome.Failed -> {
                media.disconnect()
                feedback("error", outcome.reason, outcome.message)
            }
            is VoiceStartOutcome.Started -> {
                if (!validStart(outcome, currentBinding)) {
                    media.disconnect()
                    feedback("error", "stale_generation")
                    return
                }
                session = outcome.session
                grant = outcome.grant
                startLeaseRenewal()
                transcriptSequence.clear()
                announcementSequence.clear()
                observedTurnId = null
                _state.update { it.copy(takeover = null) }
                if (outcome.session.chatContextSynced) {
                    connectMedia(outcome.grant, outcome.session, sessionFence(outcome.session))
                }
                else feedback("connecting", "chat_context_unavailable", "Waiting for the voice chat context…")
            }
        }
    }

    private suspend fun connectMedia(
        currentGrant: LiveKitVoiceGrant,
        currentSession: VoiceRestSession,
        fence: SessionFence,
    ) {
        mediaTransitionMutex.withLock {
            if (!sessionMatches(fence) || grant !== currentGrant) return
            try {
                media.connect(currentGrant)
                if (!sessionMatches(fence) || grant !== currentGrant) {
                    media.disconnect()
                    return
                }
                media.setMicrophoneEnabled(
                    currentSession.chatContextSynced &&
                        currentSession.foregroundActive &&
                        currentSession.microphoneEnabled,
                )
                if (!sessionMatches(fence) || grant !== currentGrant) {
                    media.disconnect()
                    return
                }
                feedback("greeting", "ready", "Connected. Waiting for the greeting…", mediaConnected = true)
            } catch (error: CancellationException) {
                throw error
            } catch (_: Exception) {
                if (!sessionMatches(fence) || grant !== currentGrant) {
                    media.disconnect()
                    return
                }
                val currentBinding = binding
                clearMediaSession(retainPending = true)
                feedback(
                    "error",
                    "media_error",
                    "Voice media could not connect. Check your network and try again; typed chat is still available.",
                )
                if (currentBinding != null) runCatching { api.end(currentBinding, currentSession) }
            }
        }
    }

    suspend fun setMicrophoneEnabled(enabled: Boolean) {
        val currentBinding = currentBindingOrFeedback() ?: return
        val activeSession = session ?: return feedback("error", "stale_generation")
        val fence = sessionFence(activeSession)
        runCatching { media.setMicrophoneEnabled(enabled) }
        if (!sessionMatches(fence)) return
        val result =
            sessionUpdateMutex.withLock {
                if (!sessionMatches(fence)) return@withLock null
                api.update(
                    currentBinding,
                    activeSession,
                    buildJsonObject { put("microphone_enabled", enabled) },
                )
            } ?: return
        if (!sessionMatches(fence)) return
        result.onSuccess { updated ->
            if (sameLease(activeSession, updated)) session = updated
        }.onFailure {
            if (sessionMatches(fence)) feedback("error", "stale_generation")
        }
    }

    suspend fun setSpeechMuted(muted: Boolean) {
        val currentBinding = currentBindingOrFeedback() ?: return
        val activeSession = session ?: return feedback("error", "stale_generation")
        val fence = sessionFence(activeSession)
        if (muted) media.interruptPlayout()
        val result =
            sessionUpdateMutex.withLock {
                if (!sessionMatches(fence)) return@withLock null
                api.update(
                    currentBinding,
                    activeSession,
                    buildJsonObject { put("speech_muted", muted) },
                )
            } ?: return
        if (!sessionMatches(fence)) return
        result.onSuccess { updated ->
            if (sameLease(activeSession, updated)) session = updated
        }.onFailure {
            if (sessionMatches(fence)) feedback("error", "stale_generation")
        }
    }

    suspend fun stopSpeech() {
        val currentBinding = currentBindingOrFeedback() ?: return
        val activeSession = session ?: return feedback("error", "stale_generation")
        val fence = sessionFence(activeSession)
        media.interruptPlayout()
        val stopped = api.stopSpeech(currentBinding, activeSession)
        if (sessionMatches(fence) && !stopped) feedback("error", "speech_error")
    }

    suspend fun end() {
        stopLeaseRenewal()
        val activeSession = session
        val currentBinding = binding
        clearMediaSession(retainPending = true)
        feedback("ended", "ended_by_user")
        _state.update { it.copy(terminalNotice = null) }
        if (activeSession != null && currentBinding != null) {
            sessionUpdateMutex.withLock { api.end(currentBinding, activeSession) }
        }
    }

    /** Synchronous local cleanup first; accepted ordinary tasks remain server-owned. */
    fun logout() {
        invalidateLifecycle()
        stopLeaseRenewal()
        val activeSession = session
        val currentBinding = binding
        media.disconnect()
        clearPendingFinals()
        session = null
        grant = null
        binding = null
        pendingControl = null
        connection = null
        _state.value = VoiceUiState()
        if (activeSession != null && currentBinding != null) {
            scope.launch { sessionUpdateMutex.withLock { api.end(currentBinding, activeSession) } }
        }
    }

    fun connectionLost() {
        invalidateLifecycle()
        stopLeaseRenewal()
        binding = null
        pendingControl = null
        media.disconnect()
        if (session != null) feedback("reconnecting", "network_interrupted", mediaConnected = false)
    }

    fun handleInbound(message: Inbound) {
        when (message) {
            is Inbound.ComposerState -> consumeComposer(message)
            is Inbound.VoiceControlBindingFrame -> consumeBinding(message.value)
            is Inbound.VoiceSessionStateFrame -> consumeSessionState(message.value)
            is Inbound.VoiceTurnStateFrame -> consumeTurnState(message.value)
            is Inbound.UserMessageAcked -> consumeAcknowledgement(message)
            is Inbound.VoiceSubmissionRejectedFrame -> consumeRejection(message.value)
            else -> Unit
        }
    }

    /**
     * Bind the renewable server lease to the Activity foreground without
     * manufacturing a user interaction or extending the true-idle deadline.
     */
    fun appForegroundChanged(
        active: Boolean,
        reason: String = if (active) "foreground" else "backgrounded",
    ) {
        appForeground = active
        if (!active) stopLeaseRenewal()
        val activeSession = session
        if (activeSession == null) {
            if (active) restoreServerProjectionWithoutSession()
            return
        }
        val currentBinding = binding ?: return
        val fence = sessionFence(activeSession)
        scope.launch {
            if (!sessionMatches(fence) || appForeground != active) return@launch
            if (!active) {
                media.interruptPlayout()
                runCatching { media.setMicrophoneEnabled(false) }
            }
            val fields =
                buildJsonObject {
                    put("foreground_active", active)
                    put("foreground_reason", if (active) "foreground" else reason)
                    if (!active) put("microphone_enabled", false)
                }
            val result =
                sessionUpdateMutex.withLock {
                    if (!sessionMatches(fence) || appForeground != active) return@withLock null
                    api.update(currentBinding, activeSession, fields)
                } ?: return@launch
            if (!sessionMatches(fence) || appForeground != active) return@launch
            result
                .onSuccess { updated ->
                    if (sessionMatches(fence) && sameLease(activeSession, updated)) {
                        session = updated
                        if (active) {
                            startLeaseRenewal()
                        } else {
                            feedback("suspended", reason, mediaConnected = false)
                        }
                    }
                }.onFailure {
                    if (sessionMatches(fence) && appForeground == active) {
                        stopLeaseRenewal()
                        feedback("error", "stale_generation")
                    }
                }
        }
    }

    private fun startLeaseRenewal() {
        val active = session
        if (
            !appForeground || active == null || !active.foregroundActive ||
            leaseRenewalJob?.isActive == true
        ) {
            return
        }
        leaseRenewalJob =
            scope.launch {
                while (true) {
                    delay(LEASE_RENEWAL_INTERVAL_MILLIS)
                    renewForegroundLease()
                }
            }
    }

    private fun stopLeaseRenewal() {
        leaseRenewalJob?.cancel()
        leaseRenewalJob = null
        leaseRenewalInFlight = null
    }

    private suspend fun renewForegroundLease() {
        if (!appForeground || leaseRenewalInFlight != null) return
        val activeSession = session ?: return stopLeaseRenewal()
        if (!activeSession.foregroundActive) return stopLeaseRenewal()
        val currentBinding = binding ?: return stopLeaseRenewal()
        val fence = sessionFence(activeSession)
        val renewal = Any()
        leaseRenewalInFlight = renewal
        try {
            val result =
                sessionUpdateMutex.withLock {
                    if (!appForeground || !sessionMatches(fence)) return@withLock null
                    api.update(
                        currentBinding,
                        activeSession,
                        buildJsonObject {
                            put("foreground_active", true)
                            put("foreground_reason", "foreground")
                        },
                    )
                } ?: return
            if (!appForeground || !sessionMatches(fence)) return
            result.onSuccess { updated ->
                if (sessionMatches(fence) && sameLease(activeSession, updated)) session = updated
            }.onFailure {
                if (sessionMatches(fence)) {
                    stopLeaseRenewal()
                    feedback("error", "stale_generation")
                }
            }
        } finally {
            if (leaseRenewalInFlight === renewal) leaseRenewalInFlight = null
        }
    }

    private fun sameLease(
        expected: VoiceRestSession,
        actual: VoiceRestSession,
    ): Boolean =
        actual.sessionId == expected.sessionId &&
            actual.generation == expected.generation &&
            actual.mediaGrantRevision == expected.mediaGrantRevision

    private fun sessionFence(value: VoiceRestSession): SessionFence =
        SessionFence(
            lifecycleToken = lifecycleToken,
            connectionGeneration = connection?.connectionGeneration,
            sessionId = value.sessionId,
            generation = value.generation,
            mediaGrantRevision = value.mediaGrantRevision,
        )

    private fun sessionMatches(fence: SessionFence): Boolean {
        val active = session ?: return false
        return lifecycleToken === fence.lifecycleToken &&
            connection?.connectionGeneration == fence.connectionGeneration &&
            active.sessionId == fence.sessionId &&
            active.generation == fence.generation &&
            active.mediaGrantRevision == fence.mediaGrantRevision
    }

    private fun invalidateLifecycle() {
        lifecycleToken = Any()
    }

    private fun consumeSessionState(value: com.personalailabs.astraldeep.core.protocol.VoiceSessionState) {
        val current = connection ?: return
        val active = session ?: return
        if (
            value.connectionGeneration != current.connectionGeneration ||
            value.sessionId != active.sessionId ||
            value.generation != active.generation ||
            value.mediaGrantRevision != active.mediaGrantRevision
        ) {
            return
        }
        session =
            active.copy(
                visibleChatId = value.visibleChatId,
                appliedVisibleChatId = value.visibleChatId.takeIf { value.chatContextSynced },
                chatContextRevision = value.chatContextRevision,
                appliedChatContextRevision = value.appliedChatContextRevision,
                chatContextSynced = value.chatContextSynced,
                state = value.state,
                foregroundActive = value.foregroundActive,
                speechMuted = value.speechMuted,
                microphoneEnabled = value.microphoneEnabled,
            )
        feedback(value.state, value.reason, value.message)
        when (value.state) {
            "ended" -> {
                clearMediaSession(retainPending = true)
                _state.update { it.copy(terminalNotice = null) }
            }
            "suspended", "reconnecting" -> {
                stopLeaseRenewal()
                media.disconnect()
                _state.update { it.copy(mediaConnected = false) }
            }
            else -> {
                if (value.foregroundActive && appForeground) startLeaseRenewal() else stopLeaseRenewal()
                if (value.chatContextSynced && grant != null && !_state.value.mediaConnected) {
                    val currentGrant = grant ?: return
                    val currentSession = session ?: return
                    val fence = sessionFence(currentSession)
                    scope.launch { connectMedia(currentGrant, currentSession, fence) }
                } else if (value.chatContextSynced) {
                    val currentSession = session ?: return
                    val fence = sessionFence(currentSession)
                    scope.launch {
                        if (sessionMatches(fence)) {
                            runCatching { media.setMicrophoneEnabled(value.microphoneEnabled) }
                        }
                    }
                }
            }
        }
    }

    private fun consumeTurnState(value: VoiceTurnState) {
        val current = connection ?: return
        val active = session ?: return
        if (
            value.connectionGeneration != current.connectionGeneration || value.sessionId != active.sessionId ||
            value.generation != active.generation || value.mediaGrantRevision != active.mediaGrantRevision
        ) {
            return
        }
        if (!terminalNoticeCanMoveTo(_state.value.terminalNotice, value.turnId, value.occurredAt)) {
            return
        }
        if (
            _state.value.terminalNotice?.turnId == value.turnId &&
            value.state !in setOf("succeeded", "failed", "refused", "cancelled", "abandoned")
        ) {
            return
        }
        observedTurnId = value.turnId
        if (value.state == "recognizing") media.interruptPlayout()
        val notice = reduceTerminalNotice(_state.value.terminalNotice, value)
        val speechUnavailable =
            value.state == "succeeded" && notice?.turnId == value.turnId && notice.speechUnavailable
        val phase =
            when (value.state) {
                "recognizing" -> "transcribing"
                "submitting" -> "acknowledging"
                "accepted", "processing" -> "processing"
                "waiting_on_user" -> "waiting_on_user"
                "succeeded" -> if (speechUnavailable) "error" else "speaking_result"
                "failed", "refused", "cancelled", "abandoned" -> "listening"
                else -> _state.value.phase
            }
        val reason =
            if (speechUnavailable) {
                "speech_error"
            } else if (notice?.isRequestFailure == true) {
                "voice_turn_${value.state}"
            } else {
                "ready"
            }
        _state.update {
            it.copy(
                phase = phase,
                reason = reason,
                message = value.message ?: messageFor(phase, reason),
                terminalNotice = notice,
            )
        }
    }

    private suspend fun consumeMediaEvent(event: VoiceMediaEvent) {
        when (event) {
            VoiceMediaEvent.Connected -> {
                if (session != null) _state.update { it.copy(mediaConnected = true) }
            }
            VoiceMediaEvent.Reconnecting -> {
                if (session != null) feedback("reconnecting", "network_interrupted", mediaConnected = false)
            }
            is VoiceMediaEvent.Disconnected -> {
                _state.update { it.copy(mediaConnected = false) }
                if (event.unexpected && session != null) feedback("reconnecting", "network_interrupted")
            }
            VoiceMediaEvent.Failed -> {
                if (session != null) {
                    val resultTurnId =
                        _state.value.terminalNotice
                            ?.takeIf { it.kind == VoiceTerminalNoticeKind.TEXT_RESULT_AVAILABLE }
                            ?.turnId
                    if (resultTurnId != null) {
                        markResultSpeechUnavailable(
                            "Assistant audio was unavailable. The text result remains available in the conversation.",
                            turnId = resultTurnId,
                        )
                        _state.update { it.copy(mediaConnected = false) }
                    } else {
                        feedback("error", "media_error", mediaConnected = false)
                    }
                }
            }
            is VoiceMediaEvent.AnnouncementDropped -> consumeAnnouncementDropped(event)
            is VoiceMediaEvent.Playout -> consumePlayout(event)
            is VoiceMediaEvent.Data -> consumeMediaData(event)
        }
    }

    private suspend fun consumeMediaData(event: VoiceMediaEvent.Data) {
        val maximum =
            when (event.topic) {
                VOICE_TRANSCRIPT_TOPIC -> MAX_TRANSCRIPT_BYTES
                VOICE_ANNOUNCEMENT_TOPIC -> MAX_ANNOUNCEMENT_BYTES
                else -> return
            }
        if (event.payload.size > maximum) return
        val expectedWorker = grant?.workerIdentity ?: return
        if (event.participantIdentity != expectedWorker) return
        val decoded = Wire.decode(event.payload.toString(Charsets.UTF_8))
        when {
            event.topic == VOICE_TRANSCRIPT_TOPIC && decoded is Inbound.VoiceTranscriptFrame ->
                consumeTranscript(decoded.value, event.payload.size, expectedWorker)
            event.topic == VOICE_ANNOUNCEMENT_TOPIC && decoded is Inbound.VoiceAnnouncementMediaFrame ->
                consumeAnnouncement(decoded.value, expectedWorker)
        }
    }

    private fun consumeTranscript(
        value: VoiceTranscript,
        bytes: Int,
        expectedWorker: String,
    ) {
        val active = session ?: return
        if (
            value.sessionId != active.sessionId || value.generation != active.generation ||
            value.mediaGrantRevision != active.mediaGrantRevision || value.sourceParticipantIdentity != expectedWorker
        ) {
            return
        }
        val previous = transcriptSequence[value.turnId]
        if (previous != null && value.sequence <= previous) return
        transcriptSequence[value.turnId] = value.sequence
        if (!value.final) {
            _state.update {
                it.copy(
                    phase = "transcribing",
                    transcriptPreview = "Hearing: ${value.text}",
                    transcriptFinal = false,
                )
            }
            return
        }
        if (parseFuture(value.proofExpiresAt) == null) {
            feedback("error", "proof_expired", "That spoken request expired. Please say it again.")
            return
        }
        if (pendingFinals.containsKey(value.submissionId)) return
        if (pendingFinals.size >= MAX_PENDING_FINALS || pendingFinals.values.sumOf { it.byteLength } + bytes > MAX_PENDING_BYTES) {
            feedback("error", "capacity_exhausted", "Too many spoken requests are awaiting acceptance. Please retry this one.")
            return
        }
        val pending = PendingFinal(value, bytes)
        pendingFinals[value.submissionId] = pending
        _state.update {
            it.copy(
                phase = "acknowledging",
                transcriptPreview = "Heard: ${value.text}",
                transcriptFinal = true,
                awaitingAcceptance = pendingFinals.size,
            )
        }
        pendingJobs[value.submissionId] =
            scope.launch {
                while (pendingFinals[value.submissionId] === pending) {
                    if (parseFuture(value.proofExpiresAt) == null) {
                        removePending(value.submissionId)
                        feedback("error", "proof_expired", "That spoken request was not accepted. Please say it again.")
                        break
                    }
                    sendPending(pending)
                    delay(retryDelayMillis)
                }
            }
    }

    private suspend fun consumeAnnouncement(
        value: VoiceAnnouncementMedia,
        expectedWorker: String,
    ) {
        val active = session ?: return
        if (
            value.transport != "livekit" || value.workerIdentity != expectedWorker ||
            value.sessionId != active.sessionId || value.generation != active.generation ||
            value.mediaGrantRevision != active.mediaGrantRevision
        ) {
            return
        }
        val previous = announcementSequence[active.sessionId] ?: 0
        if (value.announcementSequence <= previous) return
        val nextResultBudget = if (value.kind == "result") resultBudgetAfter(value) ?: return else null
        if (pendingAnnouncements.size >= MAX_PENDING_ANNOUNCEMENTS || !media.queueAnnouncement(value)) {
            if (value.kind == "result") {
                markResultSpeechUnavailable(
                    "Assistant audio could not be played. The text result remains available in the conversation.",
                    turnId = value.turnId ?: return,
                )
            } else {
                feedback("error", "speech_error", "Assistant audio could not be matched. Typed chat is still available.")
            }
            return
        }
        announcementSequence[active.sessionId] = value.announcementSequence
        pendingAnnouncements[value.announcementId] = PendingAnnouncement(value)
        nextResultBudget?.let { resultBudgets[requireNotNull(value.turnId)] = it }
    }

    private fun resultBudgetAfter(value: VoiceAnnouncementMedia): ResultBudget? {
        val turnId = value.turnId ?: return null
        val reserved = value.resultReservedSamplesAfter ?: return null
        if (reserved < value.durationSamples || reserved > MAX_RESULT_SAMPLES) return null
        val previous = resultBudgets[turnId]
        val locallyDeclared = (previous?.locallyDeclaredSamples ?: 0) + value.durationSamples
        if (locallyDeclared > reserved || locallyDeclared > MAX_RESULT_SAMPLES) return null
        if (
            previous != null &&
            (value.quantumIndex <= previous.quantumIndex || reserved - previous.reservedSamples < value.durationSamples)
        ) {
            return null
        }
        return ResultBudget(value.quantumIndex, reserved, locallyDeclared)
    }

    private fun consumePlayout(value: VoiceMediaEvent.Playout) {
        val pending = pendingAnnouncements[value.announcementId] ?: return
        val manifest = pending.manifest
        val current = connection ?: return
        val active = session ?: return
        if (
            value.announcementSequence != manifest.announcementSequence ||
            active.sessionId != manifest.sessionId || active.generation != manifest.generation ||
            active.mediaGrantRevision != manifest.mediaGrantRevision
        ) {
            return
        }
        when (value.phase) {
            "started" -> if (pending.phase == null) pending.phase = "started" else return
            "finished", "interrupted" -> {
                if (pending.phase != "started") return
                pending.phase = value.phase
            }
            else -> return
        }
        val observation =
            VoicePlayoutEvent(
                deviceId = current.deviceId,
                connectionGeneration = current.connectionGeneration,
                sessionId = manifest.sessionId,
                generation = manifest.generation,
                mediaGrantRevision = manifest.mediaGrantRevision,
                announcementId = manifest.announcementId,
                announcementSequence = manifest.announcementSequence,
                turnId = manifest.turnId,
                kind = manifest.kind,
                quantumRole = manifest.quantumRole,
                quantumIndex = manifest.quantumIndex,
                resultReservedSamplesAfter = manifest.resultReservedSamplesAfter,
                phase = value.phase,
                clientSequence = playoutClientSequence++,
                observedAt = clock().toString(),
            )
        playoutReporter?.invoke(observation)
        if (value.phase == "started") {
            val phase = if (manifest.kind == "result") "speaking_result" else "speaking_progress"
            feedback(phase, "ready")
            return
        }
        pendingAnnouncements.remove(manifest.announcementId)
        val settledPhase =
            when (manifest.kind) {
                "greeting" -> "listening"
                "acknowledgement", "progress" -> "processing"
                "waiting" -> "waiting_on_user"
                else -> "listening"
            }
        feedback(settledPhase, if (value.phase == "interrupted") "speech_interrupted" else "ready")
    }

    private fun consumeAnnouncementDropped(value: VoiceMediaEvent.AnnouncementDropped) {
        val pending = pendingAnnouncements[value.announcementId] ?: return
        if (pending.manifest.announcementSequence != value.announcementSequence || pending.phase != null) return
        pendingAnnouncements.remove(value.announcementId)
        if (pending.manifest.kind == "result") {
            markResultSpeechUnavailable(
                "Assistant audio was unavailable. The text result remains available in the conversation.",
                turnId = pending.manifest.turnId ?: return,
            )
        } else {
            feedback("error", "speech_error", "Assistant audio was unavailable. Typed chat is still available.")
        }
    }

    private fun markResultSpeechUnavailable(
        message: String,
        turnId: String,
    ) {
        _state.update { current ->
            if (observedTurnId != null && observedTurnId != turnId) return@update current
            val prior =
                current.terminalNotice
                    ?.takeIf {
                        it.kind == VoiceTerminalNoticeKind.TEXT_RESULT_AVAILABLE &&
                            it.turnId == turnId
                    }
            if (current.terminalNotice != null && prior == null) return@update current
            current.copy(
                phase = "error",
                reason = "speech_error",
                message = message,
                terminalNotice =
                    VoiceTerminalNotice(
                        kind = VoiceTerminalNoticeKind.TEXT_RESULT_AVAILABLE,
                        title = "Speech playback failed",
                        serverMessage = prior?.serverMessage,
                        guidance =
                            "The request completed. Its committed text result remains available " +
                                "in the conversation.",
                        turnId = turnId,
                        occurredAt = prior?.occurredAt,
                        speechUnavailable = true,
                    ),
            )
        }
    }

    private suspend fun sendPending(pending: PendingFinal) {
        val current = connection ?: return
        val currentBinding = binding ?: return
        if (
            currentBinding.connectionGeneration != current.connectionGeneration ||
            currentBinding.visibleChatId != pending.transcript.chatId
        ) {
            return
        }
        submitter?.invoke(pending.transcript, current.connectionGeneration)
    }

    private fun consumeAcknowledgement(value: Inbound.UserMessageAcked) {
        val submissionId = value.submissionId ?: return
        val pending = pendingFinals[submissionId]?.transcript ?: return
        val current = connection ?: return
        if (
            value.connectionGeneration != current.connectionGeneration || value.chatId != pending.chatId ||
            value.requestGeneration != pending.requestGeneration || value.voiceTurnId != pending.turnId
        ) {
            return
        }
        removePending(submissionId)
        _state.update {
            it.copy(
                phase = "processing",
                reason = "ready",
                message = "On it!",
            )
        }
    }

    private fun consumeRejection(value: VoiceSubmissionRejected) {
        val pending = pendingFinals[value.submissionId]?.transcript ?: return
        val current = connection ?: return
        if (
            value.connectionGeneration != current.connectionGeneration || value.sessionId != pending.sessionId ||
            value.generation != pending.generation || value.mediaGrantRevision != pending.mediaGrantRevision ||
            value.turnId != pending.turnId || value.clientTurnId != pending.clientTurnId ||
            value.requestGeneration != pending.requestGeneration || value.chatId != pending.chatId
        ) {
            return
        }
        removePending(value.submissionId)
        if (!terminalNoticeCanMoveTo(_state.value.terminalNotice, value.turnId, value.occurredAt)) return
        val notice = reduceTerminalNotice(_state.value.terminalNotice, value)
        _state.update {
            it.copy(
                phase = "listening",
                reason = "voice_submission_rejected",
                message = value.message ?: "That spoken request was not accepted.",
                terminalNotice = notice,
            )
        }
    }

    private fun removePending(submissionId: String) {
        pendingFinals.remove(submissionId)
        pendingJobs.remove(submissionId)?.cancel()
        _state.update { it.copy(awaitingAcceptance = pendingFinals.size) }
    }

    private fun clearPendingFinals() {
        pendingJobs.values.forEach(Job::cancel)
        pendingJobs.clear()
        pendingFinals.clear()
        _state.update { it.copy(awaitingAcceptance = 0) }
    }

    private fun clearMediaSession(retainPending: Boolean) {
        invalidateLifecycle()
        stopLeaseRenewal()
        media.disconnect()
        session = null
        grant = null
        transcriptSequence.clear()
        announcementSequence.clear()
        pendingAnnouncements.clear()
        resultBudgets.clear()
        observedTurnId = null
        if (!retainPending) clearPendingFinals()
        _state.update { it.copy(mediaConnected = false) }
    }

    /** Drop transient client feedback while preserving the latest server-owned controls. */
    private fun restoreServerProjectionWithoutSession() {
        _state.update { current ->
            if (!current.staleWithoutSession()) return@update current
            val composer = current.composer
            current.copy(
                phase = composer?.state ?: "off",
                reason = composer?.reason ?: "ready",
                message = composer?.let { it.message ?: messageFor(it.state, it.reason) },
                takeover = current.takeover?.takeIf { composer?.reason == "takeover_required" },
                mediaConnected = false,
            )
        }
    }

    private fun VoiceUiState.staleWithoutSession(): Boolean =
        reason == "chat_context_unavailable" ||
            (phase == "reconnecting" && reason == "network_interrupted")

    private fun currentBindingOrFeedback(): VoiceUiBinding? {
        val value = binding
        if (value == null || parseFuture(value.control.expiresAt) == null) {
            feedback("error", "auth_expired", "Voice controls are reconnecting. Try again in a moment.")
            return null
        }
        val chat = connection?.visibleChatId
        if (chat == null || chat != value.visibleChatId) {
            feedback("error", "chat_context_unavailable")
            return null
        }
        return value
    }

    private fun validStart(
        value: VoiceStartOutcome.Started,
        expected: VoiceUiBinding,
    ): Boolean =
        value.session.deviceId == expected.deviceId &&
            value.session.ownerConnectionGeneration == expected.connectionGeneration &&
            value.session.visibleChatId == expected.visibleChatId &&
            value.grant.sessionId == value.session.sessionId &&
            value.grant.generation == value.session.generation &&
            value.grant.mediaGrantRevision == value.session.mediaGrantRevision &&
            parseFuture(value.grant.expiresAt) != null

    private fun capabilityFailure(value: VoiceMediaCapability): String? =
        when {
            !value.hasMicrophone -> "no_microphone"
            !value.hasAudioOutput -> "no_audio_output"
            value.microphonePermission == "denied" -> "permission_denied"
            value.microphonePermission == "restricted" -> "permission_restricted"
            value.microphonePermission != "authorized" -> "permission_not_determined"
            else -> null
        }

    private fun feedback(
        phase: String,
        reason: String,
        message: String? = null,
        mediaConnected: Boolean? = null,
    ) {
        _state.update {
            it.copy(
                phase = phase,
                reason = reason,
                message = message ?: messageFor(phase, reason),
                mediaConnected = mediaConnected ?: it.mediaConnected,
            )
        }
    }

    private fun parseFuture(value: String?): Instant? {
        val parsed = value?.let { runCatching { Instant.parse(it) }.getOrNull() } ?: return null
        return parsed.takeIf { it.isAfter(clock()) }
    }

    fun close() {
        invalidateLifecycle()
        stopLeaseRenewal()
        media.disconnect()
        clearPendingFinals()
        scope.cancel()
    }

    companion object {
        private const val MAX_TRANSCRIPT_BYTES = 12 * 1024
        private const val MAX_ANNOUNCEMENT_BYTES = 4 * 1024
        private const val MAX_PENDING_FINALS = 4
        private const val MAX_PENDING_BYTES = 48 * 1024
        private const val MAX_PENDING_ANNOUNCEMENTS = 8
        private const val MAX_RESULT_SAMPLES = 720_000
        private const val LEASE_RENEWAL_INTERVAL_MILLIS = 20_000L
        private val BINDING_VALUE = Regex("^[A-Za-z0-9._~-]+$")

        private fun isUuid4(value: String): Boolean {
            val parsed = runCatching { UUID.fromString(value) }.getOrNull()
            return parsed?.version() == 4 && parsed.toString() == value
        }

        private fun messageFor(
            phase: String,
            reason: String,
        ): String =
            when (reason) {
                "permission_not_determined" -> "Allow microphone access to start a voice conversation."
                "permission_denied" -> "Microphone permission was denied. Allow it in Settings or keep typing."
                "permission_restricted" -> "Microphone access is restricted. You can keep typing."
                "no_microphone" -> "No microphone is available. Connect one or keep typing."
                "no_audio_output" -> "No audio output is available. Connect one or keep typing."
                "media_error" ->
                    "Voice media could not connect. Check your network and try again; typed chat is still available."
                "takeover_required" -> "Voice is active on another device. Choose Take over to continue here."
                "idle_expired" -> "Voice ended after five idle minutes. Accepted requests keep running."
                "chat_context_unavailable" -> "Waiting for the voice chat context…"
                "network_interrupted" -> "Voice connection was interrupted. Typed chat is still available."
                "ended_by_user" -> "Voice conversation ended. Accepted requests keep running."
                "proof_expired" -> "That spoken request expired before acceptance. Please say it again."
                else ->
                    when (phase) {
                        "connecting" -> "Connecting voice…"
                        "greeting" -> "Connected. Waiting for the greeting…"
                        "listening" -> "Listening…"
                        "speech_detected" -> "I hear you…"
                        "transcribing" -> "Understanding what you said…"
                        "acknowledging" -> "Submitting your spoken request…"
                        "processing" -> "Working on it…"
                        "waiting_on_user" -> "Waiting for your response…"
                        "speaking_progress" -> "Speaking a progress update…"
                        "speaking_result" -> "Speaking the completed result…"
                        "muted" -> "Assistant speech is muted."
                        "ended" -> "Voice conversation ended."
                        else -> "Voice is available."
                    }
            }
    }
}

/** Strict no-store HTTP implementation of the voice control REST contract. */
class OkHttpVoiceControlApi(
    private val baseUrl: String,
    private val client: OkHttpClient =
        OkHttpClient.Builder()
            .connectTimeout(10, TimeUnit.SECONDS)
            .readTimeout(20, TimeUnit.SECONDS)
            .writeTimeout(20, TimeUnit.SECONDS)
            .build(),
) : VoiceControlApi {
    private data class HttpResult(val status: Int, val body: JsonObject?)

    private val json = Json { ignoreUnknownKeys = false }

    override suspend fun start(
        binding: VoiceUiBinding,
        activationId: String,
        capability: VoiceMediaCapability,
    ): VoiceStartOutcome =
        startRequest(
            binding,
            "/api/voice/sessions",
            buildActivationBody(binding, activationId, capability),
        )

    override suspend fun takeover(
        binding: VoiceUiBinding,
        activationId: String,
        target: VoiceTakeoverTarget,
        capability: VoiceMediaCapability,
    ): VoiceStartOutcome =
        startRequest(
            binding,
            "/api/voice/sessions/${target.sessionId}/takeover",
            buildJsonObject {
                buildActivationBody(binding, activationId, capability).forEach(::put)
                put("expected_generation", target.generation)
                put("expected_media_grant_revision", target.mediaGrantRevision)
            },
        )

    override suspend fun update(
        binding: VoiceUiBinding,
        session: VoiceRestSession,
        fields: JsonObject,
    ): Result<VoiceRestSession> {
        val body =
            buildJsonObject {
                put("expected_generation", session.generation)
                put("expected_media_grant_revision", session.mediaGrantRevision)
                fields.forEach(::put)
            }
        val result = request(binding, "/api/voice/sessions/${session.sessionId}", "PATCH", body)
        val parsed = result.body?.let(::parseSession)
        return if (result.status in 200..299 && parsed != null) Result.success(parsed)
        else Result.failure(VoiceControlFailure(problemCode(result.body)))
    }

    override suspend fun stopSpeech(
        binding: VoiceUiBinding,
        session: VoiceRestSession,
    ): Boolean {
        val result =
            request(
                binding,
                "/api/voice/sessions/${session.sessionId}/speech/stop",
                "POST",
                generationBody(session),
            )
        return result.status == 202
    }

    override suspend fun end(
        binding: VoiceUiBinding,
        session: VoiceRestSession,
    ): Boolean {
        val path =
            "/api/voice/sessions/${session.sessionId}?expected_generation=${session.generation}" +
                "&expected_media_grant_revision=${session.mediaGrantRevision}"
        return request(binding, path, "DELETE", null).status == 204
    }

    private suspend fun startRequest(
        binding: VoiceUiBinding,
        path: String,
        body: JsonObject,
    ): VoiceStartOutcome {
        val result = request(binding, path, "POST", body)
        if (result.status == 409 && problemCode(result.body) == "voice_takeover_required") {
            val owner = result.body?.obj("owner")
            val target = owner?.let(::parseTakeoverTarget)
            if (target != null) {
                return VoiceStartOutcome.TakeoverRequired(target, result.body?.str("message"))
            }
        }
        if (result.status !in 200..299) {
            return VoiceStartOutcome.Failed(problemCode(result.body), result.body?.str("message"))
        }
        val session = result.body?.obj("session")?.let(::parseSession)
        val grant = result.body?.obj("grant")?.let(::parseGrant)
        if (session == null || grant == null || grant.sessionId != session.sessionId ||
            grant.generation != session.generation || grant.mediaGrantRevision != session.mediaGrantRevision
        ) {
            return VoiceStartOutcome.Failed("voice_unavailable")
        }
        return VoiceStartOutcome.Started(session, grant)
    }

    private suspend fun request(
        binding: VoiceUiBinding,
        path: String,
        method: String,
        body: JsonObject?,
    ): HttpResult =
        withContext(Dispatchers.IO) {
            val builder =
                Request.Builder()
                    .url(baseUrl.trimEnd('/') + path)
                    .header("Authorization", "Bearer ${binding.token}")
                    .header("X-Astral-Device-Id", binding.deviceId)
                    .header("X-Astral-Connection-Generation", binding.connectionGeneration)
                    .header("X-Astral-Voice-Control-Binding", binding.control.binding)
                    .header("Cache-Control", "no-store")
            val requestBody = body?.toString()?.toRequestBody(JSON_MEDIA_TYPE)
            when (method) {
                "POST" -> builder.post(requireNotNull(requestBody))
                "PATCH" -> builder.patch(requireNotNull(requestBody))
                "DELETE" -> builder.delete()
                else -> error("unsupported voice method")
            }
            runCatching {
                client.newCall(builder.build()).execute().use { response ->
                    val raw = if (response.code == 204) "" else response.body?.string().orEmpty()
                    val parsed =
                        raw.takeIf { it.isNotBlank() }
                            ?.let { runCatching { json.parseToJsonElement(it) as? JsonObject }.getOrNull() }
                    HttpResult(response.code, parsed)
                }
            }.getOrElse { HttpResult(0, null) }
        }

    private fun buildActivationBody(
        binding: VoiceUiBinding,
        activationId: String,
        capability: VoiceMediaCapability,
    ): JsonObject =
        buildJsonObject {
            put("device_id", binding.deviceId)
            put("device_kind", "android")
            put("visible_chat_id", binding.visibleChatId)
            put("activation_id", activationId)
            putJsonObject("capability") {
                put("has_microphone", capability.hasMicrophone)
                put("has_audio_output", capability.hasAudioOutput)
                put("microphone_permission", capability.microphonePermission)
                put("full_duplex", capability.fullDuplex)
                put("transport", "livekit")
            }
            put("foreground_active", true)
        }

    private fun generationBody(session: VoiceRestSession): JsonObject =
        buildJsonObject {
            put("expected_generation", session.generation)
            put("expected_media_grant_revision", session.mediaGrantRevision)
        }

    private fun parseSession(root: JsonObject): VoiceRestSession? {
        val appliedVisible = root.nullableUuid("applied_visible_chat_id") ?: return null
        val appliedRevision = root.nullablePositiveInt("applied_chat_context_revision") ?: return null
        return VoiceRestSession(
            sessionId = root.uuid("session_id") ?: return null,
            deviceId = root.uuid("device_id") ?: return null,
            ownerConnectionGeneration = root.uuid("owner_connection_generation") ?: return null,
            visibleChatId = root.uuid("visible_chat_id") ?: return null,
            appliedVisibleChatId = appliedVisible.value,
            generation = root.positiveInt("generation") ?: return null,
            mediaGrantRevision = root.positiveInt("media_grant_revision") ?: return null,
            chatContextRevision = root.positiveInt("chat_context_revision") ?: return null,
            appliedChatContextRevision = appliedRevision.value,
            chatContextSynced = root.bool("chat_context_synced") ?: return null,
            state = root.str("state")?.takeIf { it in REST_SESSION_STATES } ?: return null,
            foregroundActive = root.bool("foreground_active") ?: return null,
            speechMuted = root.bool("speech_muted") ?: return null,
            microphoneEnabled = root.bool("microphone_enabled") ?: return null,
        )
    }

    private fun parseGrant(root: JsonObject): LiveKitVoiceGrant? {
        if (root.str("transport") != "livekit") return null
        val url = root.str("url")?.takeIf(::isWebSocketUrl) ?: return null
        val token = root.str("join_token")?.takeIf { it.length in 32..8192 } ?: return null
        return LiveKitVoiceGrant(
            grantId = root.opaque("grant_id") ?: return null,
            sessionId = root.uuid("session_id") ?: return null,
            generation = root.positiveInt("generation") ?: return null,
            mediaGrantRevision = root.positiveInt("media_grant_revision") ?: return null,
            expiresAt = root.str("expires_at")?.takeIf(::isTimestamp) ?: return null,
            url = url,
            joinToken = token,
            roomName = root.opaque("room_name") ?: return null,
            participantIdentity = root.opaque("participant_identity") ?: return null,
            workerIdentity = root.opaque("worker_identity") ?: return null,
        )
    }

    private fun parseTakeoverTarget(root: JsonObject): VoiceTakeoverTarget? {
        return VoiceTakeoverTarget(
            sessionId = root.uuid("session_id") ?: return null,
            deviceKind = root.str("device_kind") ?: return null,
            deviceLabel = root.str("device_label"),
            generation = root.positiveInt("generation") ?: return null,
            mediaGrantRevision = root.positiveInt("media_grant_revision") ?: return null,
        )
    }

    private fun problemCode(root: JsonObject?): String = root?.str("code") ?: "network_interrupted"

    private data class NullableValue<out T>(val value: T?)

    private fun JsonObject.str(key: String): String? =
        (this[key] as? JsonPrimitive)?.takeIf { it.isString }?.contentOrNull

    private fun JsonObject.bool(key: String): Boolean? =
        (this[key] as? JsonPrimitive)?.takeIf { !it.isString }?.booleanOrNull

    private fun JsonObject.positiveInt(key: String): Int? =
        (this[key] as? JsonPrimitive)?.takeIf { !it.isString }?.intOrNull?.takeIf { it > 0 }

    private fun JsonObject.obj(key: String): JsonObject? = this[key] as? JsonObject

    private fun JsonObject.uuid(key: String): String? = str(key)?.takeIf(::isUuid4)

    private fun JsonObject.opaque(key: String): String? =
        str(key)?.takeIf { it.length in 1..128 && OPAQUE.matches(it) }

    private fun JsonObject.nullableUuid(key: String): NullableValue<String>? {
        val value = this[key] ?: return null
        if (value is JsonNull) return NullableValue(null)
        return uuid(key)?.let(::NullableValue)
    }

    private fun JsonObject.nullablePositiveInt(key: String): NullableValue<Int>? {
        val value = this[key] ?: return null
        if (value is JsonNull) return NullableValue(null)
        return positiveInt(key)?.let(::NullableValue)
    }

    private class VoiceControlFailure(message: String) : RuntimeException(message)

    companion object {
        private val JSON_MEDIA_TYPE = "application/json".toMediaType()
        private val OPAQUE = Regex("^[A-Za-z0-9._:-]+$")
        private val REST_SESSION_STATES = setOf("starting", "active", "suspended", "reconnecting", "ending", "ended", "error")

        private fun isUuid4(value: String): Boolean {
            val parsed = runCatching { UUID.fromString(value) }.getOrNull()
            return parsed?.version() == 4 && parsed.toString() == value
        }

        private fun isTimestamp(value: String): Boolean = runCatching { Instant.parse(value) }.isSuccess

        private fun isWebSocketUrl(value: String): Boolean =
            runCatching { URI(value).scheme in setOf("ws", "wss") }.getOrDefault(false)
    }
}
