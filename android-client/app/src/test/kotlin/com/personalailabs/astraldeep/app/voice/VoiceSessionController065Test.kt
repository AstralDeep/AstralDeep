package com.personalailabs.astraldeep.app.voice

import com.personalailabs.astraldeep.core.protocol.Inbound
import com.personalailabs.astraldeep.core.protocol.VoiceComposerModel
import com.personalailabs.astraldeep.core.protocol.VoiceControl
import com.personalailabs.astraldeep.core.protocol.VoiceControlBinding
import com.personalailabs.astraldeep.core.protocol.VoiceOwnerDevice
import com.personalailabs.astraldeep.core.protocol.VoiceSessionState
import com.personalailabs.astraldeep.core.protocol.VoiceSpeechOutcome
import com.personalailabs.astraldeep.core.protocol.VoiceSubmissionRejected
import com.personalailabs.astraldeep.core.protocol.VoiceTurnState
import kotlinx.coroutines.CompletableDeferred
import kotlinx.coroutines.ExperimentalCoroutinesApi
import kotlinx.coroutines.flow.Flow
import kotlinx.coroutines.flow.MutableSharedFlow
import kotlinx.coroutines.launch
import kotlinx.coroutines.test.TestScope
import kotlinx.coroutines.test.advanceTimeBy
import kotlinx.coroutines.test.runCurrent
import kotlinx.coroutines.test.runTest
import kotlinx.serialization.json.JsonObject
import kotlinx.serialization.json.booleanOrNull
import kotlinx.serialization.json.contentOrNull
import kotlinx.serialization.json.jsonPrimitive
import kotlin.test.Test
import kotlin.test.assertEquals
import kotlin.test.assertFalse
import kotlin.test.assertTrue

@OptIn(ExperimentalCoroutinesApi::class)
class VoiceSessionController065Test {
    @Test
    fun newConnectionGenerationClearsStaleControlsAndAcceptsFreshComposerRevision() =
        runTest {
            val fixture = fixture(this)
            fixture.controller.consumeComposer(
                Inbound.ComposerState(7, CONNECTION_ID, composer(state = "off")),
            )
            assertEquals("voice_session_start", fixture.controller.state.value.composer?.controls?.single()?.action)
            fixture.controller.activationFailed(
                "chat_context_unavailable",
                "Waiting for the voice chat context…",
            )

            fixture.controller.installUiConnection("user-token", DEVICE_ID, OTHER_CONNECTION_ID, CHAT_ID)

            assertEquals(null, fixture.controller.state.value.composer)
            assertEquals("off", fixture.controller.state.value.phase)
            assertEquals("ready", fixture.controller.state.value.reason)
            assertEquals(null, fixture.controller.state.value.message)
            fixture.controller.consumeComposer(
                Inbound.ComposerState(
                    0,
                    OTHER_CONNECTION_ID,
                    composer(
                        state = "listening",
                        foregroundActive = true,
                        microphoneEnabled = true,
                    ),
                ),
            )

            assertEquals("listening", fixture.controller.state.value.phase)
            assertEquals("voice_session_end", fixture.controller.state.value.composer?.controls?.single()?.action)
        }

    @Test
    fun chatlessNavigationClearsStaleNoSessionFeedbackAndRebindsFreshChat() =
        runTest {
            val fixture = fixture(this)
            fixture.controller.consumeComposer(
                Inbound.ComposerState(0, CONNECTION_ID, composer(state = "off")),
            )
            fixture.controller.activationFailed(
                "chat_context_unavailable",
                "Waiting for the voice chat context…",
            )

            fixture.controller.updateVisibleChatLocally(null)

            assertEquals("off", fixture.controller.state.value.phase)
            assertEquals("ready", fixture.controller.state.value.reason)
            assertEquals("Voice is available.", fixture.controller.state.value.message)
            assertEquals("voice_session_start", fixture.controller.state.value.composer?.controls?.single()?.action)

            fixture.controller.updateVisibleChatLocally(OTHER_CHAT_ID)
            fixture.api.startOutcome =
                VoiceStartOutcome.Started(
                    session().copy(visibleChatId = OTHER_CHAT_ID, appliedVisibleChatId = OTHER_CHAT_ID),
                    grant(),
                )
            fixture.controller.activate(capability())

            assertEquals(1, fixture.api.startCalls)
        }

    @Test
    fun chatlessNavigationImmediatelyPausesAnActiveSessionUntilContextReturns() =
        runTest {
            val fixture = fixture(this)
            fixture.controller.activate(capability())

            fixture.controller.updateVisibleChatLocally(null)
            runCurrent()

            assertEquals(listOf(true, false), fixture.media.microphoneChanges)
            assertEquals(1, fixture.media.interruptCalls)
            assertEquals("connecting", fixture.controller.state.value.phase)
            assertEquals("chat_context_unavailable", fixture.controller.state.value.reason)

            fixture.controller.updateVisibleChatLocally(OTHER_CHAT_ID)
            runCurrent()

            assertEquals(OTHER_CHAT_ID, fixture.api.updatedSession?.visibleChatId)
            assertEquals(listOf(true, false, false, true), fixture.media.microphoneChanges)
        }

    @Test
    fun foregroundWithoutLocalSessionRestoresTheLatestServerProjection() =
        runTest {
            val fixture = fixture(this)
            fixture.controller.consumeComposer(
                Inbound.ComposerState(0, CONNECTION_ID, composer(state = "off")),
            )
            fixture.controller.activationFailed(
                "chat_context_unavailable",
                "Waiting for the voice chat context…",
            )

            fixture.controller.appForegroundChanged(active = false, reason = "backgrounded")
            fixture.controller.appForegroundChanged(active = true)

            assertEquals("off", fixture.controller.state.value.phase)
            assertEquals("ready", fixture.controller.state.value.reason)
            assertEquals("Voice is available.", fixture.controller.state.value.message)
            assertEquals("voice_session_start", fixture.controller.state.value.composer?.controls?.single()?.action)
        }

    @Test
    fun foregroundWithoutLocalSessionDoesNotHideActionableCapabilityFailure() =
        runTest {
            val fixture = fixture(this)
            fixture.controller.consumeComposer(
                Inbound.ComposerState(0, CONNECTION_ID, composer(state = "off")),
            )
            fixture.controller.activationFailed(
                "permission_denied",
                "Microphone permission was denied. Allow it in Settings or keep typing.",
            )

            fixture.controller.appForegroundChanged(active = false, reason = "backgrounded")
            fixture.controller.appForegroundChanged(active = true)

            assertEquals("error", fixture.controller.state.value.phase)
            assertEquals("permission_denied", fixture.controller.state.value.reason)
            assertEquals(
                "Microphone permission was denied. Allow it in Settings or keep typing.",
                fixture.controller.state.value.message,
            )
        }

    @Test
    fun delayedChatContextUpdateCannotResurrectAUserEndedSession() =
        runTest {
            val fixture = fixture(this)
            fixture.controller.activate(capability())
            val updateStarted = CompletableDeferred<Unit>()
            val releaseUpdate = CompletableDeferred<Unit>()
            fixture.api.updateStarted = updateStarted
            fixture.api.releaseUpdate = releaseUpdate

            fixture.controller.updateVisibleChatLocally(OTHER_CHAT_ID)
            updateStarted.await()
            val endJob = launch { fixture.controller.end() }
            runCurrent()

            assertEquals("ended", fixture.controller.state.value.phase)
            assertEquals("ended_by_user", fixture.controller.state.value.reason)
            assertFalse(fixture.controller.state.value.mediaConnected)

            releaseUpdate.complete(Unit)
            endJob.join()
            runCurrent()

            assertEquals(1, fixture.api.endCalls)
            assertEquals("ended", fixture.controller.state.value.phase)
            assertEquals("ended_by_user", fixture.controller.state.value.reason)
            assertEquals(listOf(true, false), fixture.media.microphoneChanges)
            fixture.media.emit(VoiceMediaEvent.Connected)
            runCurrent()
            assertFalse(fixture.controller.state.value.mediaConnected)
        }

    @Test
    fun delayedForegroundUpdateCannotReplaceEndedFeedback() =
        runTest {
            val fixture = fixture(this)
            fixture.controller.activate(capability())
            val updateStarted = CompletableDeferred<Unit>()
            val releaseUpdate = CompletableDeferred<Unit>()
            fixture.api.updateStarted = updateStarted
            fixture.api.releaseUpdate = releaseUpdate

            fixture.controller.appForegroundChanged(active = false, reason = "backgrounded")
            updateStarted.await()
            val endJob = launch { fixture.controller.end() }
            runCurrent()

            assertEquals("ended", fixture.controller.state.value.phase)
            releaseUpdate.complete(Unit)
            endJob.join()
            runCurrent()

            assertEquals(1, fixture.api.endCalls)
            assertEquals("ended", fixture.controller.state.value.phase)
            assertEquals("ended_by_user", fixture.controller.state.value.reason)
            assertFalse(fixture.controller.state.value.mediaConnected)
        }

    @Test
    fun delayedMediaConnectCannotPublishGreetingAfterEnd() =
        runTest {
            val fixture = fixture(this)
            val connectStarted = CompletableDeferred<Unit>()
            val releaseConnect = CompletableDeferred<Unit>()
            fixture.media.connectStarted = connectStarted
            fixture.media.releaseConnect = releaseConnect

            val activationJob = launch { fixture.controller.activate(capability()) }
            connectStarted.await()
            fixture.controller.end()

            assertEquals("ended", fixture.controller.state.value.phase)
            assertFalse(fixture.controller.state.value.mediaConnected)

            releaseConnect.complete(Unit)
            activationJob.join()
            runCurrent()

            assertEquals(1, fixture.api.endCalls)
            assertEquals("ended", fixture.controller.state.value.phase)
            assertEquals("ended_by_user", fixture.controller.state.value.reason)
            assertTrue(fixture.media.microphoneChanges.isEmpty())
            assertFalse(fixture.controller.state.value.mediaConnected)
        }

    @Test
    fun permissionAndRealAudioDeviceFailuresStayVisibleAndNeverStartMedia() =
        runTest {
            val fixture = fixture(this)

            fixture.controller.activate(capability(hasMicrophone = false))
            assertEquals("no_microphone", fixture.controller.state.value.reason)
            fixture.controller.activate(capability(hasAudioOutput = false))
            assertEquals("no_audio_output", fixture.controller.state.value.reason)
            fixture.controller.activate(capability(permission = "denied"))
            assertEquals("permission_denied", fixture.controller.state.value.reason)

            assertEquals(0, fixture.api.startCalls)
            assertEquals(0, fixture.media.connectCalls)
        }

    @Test
    fun activationUsesBoundRestSessionThenConnectsLiveKitAndPublishesOnlyMicrophone() =
        runTest {
            val fixture = fixture(this)

            fixture.controller.activate(capability())

            assertEquals(1, fixture.api.startCalls)
            assertEquals(1, fixture.media.connectCalls)
            assertEquals(listOf(true), fixture.media.microphoneChanges)
            assertEquals("greeting", fixture.controller.state.value.phase)
            assertTrue(fixture.controller.state.value.mediaConnected)
        }

    @Test
    fun contextUnsyncedActivationDoesNotOpenMediaUntilTheServerAppliesTheChat() =
        runTest {
            val fixture = fixture(this)
            fixture.api.startOutcome =
                VoiceStartOutcome.Started(
                    session().copy(
                        appliedVisibleChatId = null,
                        appliedChatContextRevision = null,
                        chatContextSynced = false,
                        microphoneEnabled = false,
                    ),
                    grant(),
                )

            fixture.controller.activate(capability())

            assertEquals(0, fixture.media.connectCalls)
            assertTrue(fixture.media.microphoneChanges.isEmpty())
            assertEquals("chat_context_unavailable", fixture.controller.state.value.reason)

            fixture.controller.handleInbound(
                Inbound.VoiceSessionStateFrame(
                    sessionState(
                        chatContextSynced = true,
                        microphoneEnabled = true,
                        state = "active",
                    ),
                ),
            )
            runCurrent()

            assertEquals(1, fixture.media.connectCalls)
            assertEquals(listOf(true), fixture.media.microphoneChanges)
            assertEquals("greeting", fixture.controller.state.value.phase)
        }

    @Test
    fun microphoneAndSpeechControlsUseGenerationFencedRestUpdates() =
        runTest {
            val fixture = fixture(this)
            fixture.controller.activate(capability())

            fixture.controller.setMicrophoneEnabled(false)
            fixture.controller.setSpeechMuted(true)

            assertEquals(listOf(true, false), fixture.media.microphoneChanges)
            assertEquals(false, fixture.api.updatedSession?.microphoneEnabled)
            assertEquals(true, fixture.api.updatedSession?.speechMuted)
        }

    @Test
    fun mediaConnectionFailureEndsTheServerSessionAndKeepsTypedChatAvailable() =
        runTest {
            val fixture = fixture(this)
            fixture.media.connectError = IllegalStateException("synthetic media failure")

            fixture.controller.activate(capability())

            assertEquals(1, fixture.api.endCalls)
            assertEquals("error", fixture.controller.state.value.phase)
            assertEquals("media_error", fixture.controller.state.value.reason)
            assertEquals(
                "Voice media could not connect. Check your network and try again; typed chat is still available.",
                fixture.controller.state.value.message,
            )
            assertFalse(fixture.controller.state.value.mediaConnected)
        }

    @Test
    fun greetingAndProgressManifestsComeOnlyFromTheExpectedWorkerAndGrant() =
        runTest {
            val fixture = fixture(this)
            val reports = mutableListOf<com.personalailabs.astraldeep.core.protocol.VoicePlayoutEvent>()
            fixture.controller.setPlayoutReporter { reports += it; true }
            fixture.controller.activate(capability())
            fixture.media.emit(
                VoiceMediaEvent.Data(
                    VOICE_ANNOUNCEMENT_TOPIC,
                    "unexpected-worker",
                    announcement(kind = "acknowledgement", turnId = TURN_ID).encodeToByteArray(),
                ),
            )
            runCurrent()
            assertEquals("greeting", fixture.controller.state.value.phase)
            assertTrue(fixture.media.announcements.isEmpty())

            fixture.media.emit(
                VoiceMediaEvent.Data(
                    VOICE_ANNOUNCEMENT_TOPIC,
                    WORKER,
                    announcement(kind = "acknowledgement", turnId = TURN_ID).encodeToByteArray(),
                ),
            )
            runCurrent()
            assertEquals(1, fixture.media.announcements.size)
            assertEquals("greeting", fixture.controller.state.value.phase)

            fixture.media.emit(VoiceMediaEvent.Playout(ANNOUNCEMENT_ID, 1, "started"))
            runCurrent()
            assertEquals("speaking_progress", fixture.controller.state.value.phase)
            assertEquals(listOf("started"), reports.map { it.phase })
            assertEquals(0, reports.single().clientSequence)

            fixture.media.emit(VoiceMediaEvent.Playout(ANNOUNCEMENT_ID, 1, "finished"))
            runCurrent()
            assertEquals("processing", fixture.controller.state.value.phase)
            assertEquals(listOf("started", "finished"), reports.map { it.phase })
            assertEquals(listOf(0, 1), reports.map { it.clientSequence })
        }

    @Test
    fun playoutLifecycleIsCorrelatedOrderedAndContentFree() =
        runTest {
            val fixture = fixture(this)
            val reports = mutableListOf<com.personalailabs.astraldeep.core.protocol.VoicePlayoutEvent>()
            fixture.controller.setPlayoutReporter { reports += it; true }
            fixture.controller.activate(capability())
            fixture.media.emit(
                VoiceMediaEvent.Data(
                    VOICE_ANNOUNCEMENT_TOPIC,
                    WORKER,
                    announcement(kind = "result", turnId = TURN_ID, quantumRole = "result_opening")
                        .encodeToByteArray(),
                ),
            )
            runCurrent()

            fixture.media.emit(VoiceMediaEvent.Playout(ANNOUNCEMENT_ID, 2, "started"))
            fixture.media.emit(VoiceMediaEvent.Playout("00000000-0000-4000-8000-000000000099", 1, "started"))
            fixture.media.emit(VoiceMediaEvent.Playout(ANNOUNCEMENT_ID, 1, "finished"))
            runCurrent()
            assertTrue(reports.isEmpty())

            fixture.media.emit(VoiceMediaEvent.Playout(ANNOUNCEMENT_ID, 1, "started"))
            fixture.media.emit(VoiceMediaEvent.Playout(ANNOUNCEMENT_ID, 1, "started"))
            fixture.media.emit(VoiceMediaEvent.Playout(ANNOUNCEMENT_ID, 1, "interrupted"))
            fixture.media.emit(VoiceMediaEvent.Playout(ANNOUNCEMENT_ID, 1, "finished"))
            runCurrent()

            assertEquals(listOf("started", "interrupted"), reports.map { it.phase })
            assertEquals("result", reports.first().kind)
            assertEquals("result_opening", reports.first().quantumRole)
            assertEquals(36_000, reports.first().resultReservedSamplesAfter)
            assertEquals("speech_interrupted", fixture.controller.state.value.reason)
            assertTrue(reports.none { it.toString().contains("schedule", ignoreCase = true) })
        }

    @Test
    fun unmatchedManifestDropSettlesLocalStateWithoutFabricatingPlayoutEvidence() =
        runTest {
            val fixture = fixture(this)
            val reports = mutableListOf<com.personalailabs.astraldeep.core.protocol.VoicePlayoutEvent>()
            fixture.controller.setPlayoutReporter { reports += it; true }
            fixture.controller.activate(capability())
            fixture.media.emit(
                VoiceMediaEvent.Data(
                    VOICE_ANNOUNCEMENT_TOPIC,
                    WORKER,
                    announcement(kind = "acknowledgement", turnId = TURN_ID).encodeToByteArray(),
                ),
            )
            runCurrent()

            fixture.media.emit(VoiceMediaEvent.AnnouncementDropped(ANNOUNCEMENT_ID, 1))
            runCurrent()

            assertEquals("error", fixture.controller.state.value.phase)
            assertEquals("speech_error", fixture.controller.state.value.reason)
            assertTrue(reports.isEmpty())
        }

    @Test
    fun mediaQueueRefusalDoesNotConsumeTheAnnouncementSequence() =
        runTest {
            val fixture = fixture(this)
            fixture.controller.activate(capability())
            fixture.media.acceptAnnouncements = false
            val payload = announcement(kind = "acknowledgement", turnId = TURN_ID).encodeToByteArray()
            fixture.media.emit(VoiceMediaEvent.Data(VOICE_ANNOUNCEMENT_TOPIC, WORKER, payload))
            runCurrent()
            assertEquals("speech_error", fixture.controller.state.value.reason)
            assertTrue(fixture.media.announcements.isEmpty())

            fixture.media.acceptAnnouncements = true
            fixture.media.emit(VoiceMediaEvent.Data(VOICE_ANNOUNCEMENT_TOPIC, WORKER, payload))
            runCurrent()
            assertEquals(1, fixture.media.announcements.size)
        }

    @Test
    fun resultQuantaEnforceMonotonicPerTurnAggregateReservation() =
        runTest {
            val fixture = fixture(this)
            fixture.controller.activate(capability())
            fixture.media.emit(
                VoiceMediaEvent.Data(
                    VOICE_ANNOUNCEMENT_TOPIC,
                    WORKER,
                    announcement(
                        kind = "result",
                        turnId = TURN_ID,
                        quantumRole = "result_opening",
                        reservedSamples = 12_000,
                    ).encodeToByteArray(),
                ),
            )
            fixture.media.emit(
                VoiceMediaEvent.Data(
                    VOICE_ANNOUNCEMENT_TOPIC,
                    WORKER,
                    announcement(
                        kind = "result",
                        turnId = TURN_ID,
                        quantumRole = "result_continuation",
                        quantumIndex = 1,
                        announcementId = "00000000-0000-4000-8000-000000000019",
                        announcementSequence = 2,
                        durationSamples = 96_000,
                        reservedSamples = 108_000,
                    ).encodeToByteArray(),
                ),
            )
            fixture.media.emit(
                VoiceMediaEvent.Data(
                    VOICE_ANNOUNCEMENT_TOPIC,
                    WORKER,
                    announcement(
                        kind = "result",
                        turnId = TURN_ID,
                        quantumRole = "result_continuation",
                        quantumIndex = 2,
                        announcementId = "00000000-0000-4000-8000-000000000029",
                        announcementSequence = 3,
                        durationSamples = 96_000,
                        reservedSamples = 108_000,
                    ).encodeToByteArray(),
                ),
            )
            runCurrent()

            assertEquals(listOf(0, 1), fixture.media.announcements.map { it.quantumIndex })
            assertEquals(listOf(12_000, 108_000), fixture.media.announcements.map { it.resultReservedSamplesAfter })
        }

    @Test
    fun stopAndEndFenceTheCurrentSessionWithoutCancellingOrdinaryWork() =
        runTest {
            val fixture = fixture(this)
            fixture.controller.activate(capability())

            fixture.controller.stopSpeech()
            fixture.controller.end()

            assertEquals(1, fixture.api.stopCalls)
            assertEquals(1, fixture.api.endCalls)
            assertTrue(fixture.media.interruptCalls >= 1)
            assertTrue(fixture.media.disconnectCalls >= 1)
            assertEquals("ended", fixture.controller.state.value.phase)
            assertEquals("ended_by_user", fixture.controller.state.value.reason)
        }

    @Test
    fun explicitTakeoverUsesTheConflictingOwnerGenerationAndFreshGrant() =
        runTest {
            val fixture = fixture(this)
            fixture.api.startOutcome =
                VoiceStartOutcome.TakeoverRequired(
                    VoiceTakeoverTarget(SESSION_ID, "ios", "Sam's iPhone", 3, 4),
                    "Voice is active elsewhere.",
                )

            fixture.controller.activate(capability())
            assertEquals("takeover_required", fixture.controller.state.value.reason)
            assertEquals(3, fixture.controller.state.value.takeover?.generation)

            fixture.api.takeoverOutcome = VoiceStartOutcome.Started(session(), grant())
            fixture.controller.takeOver(capability())
            assertEquals(1, fixture.api.takeoverCalls)
            assertEquals("greeting", fixture.controller.state.value.phase)
            assertEquals(null, fixture.controller.state.value.takeover)
        }

    @Test
    fun fiveMinuteServerIdleDispositionTearsDownMediaButKeepsItsHonestReason() =
        runTest {
            val fixture = fixture(this)
            fixture.controller.activate(capability())

            fixture.controller.handleInbound(
                Inbound.VoiceSessionStateFrame(
                    VoiceSessionState(
                        sessionId = SESSION_ID,
                        connectionGeneration = CONNECTION_ID,
                        generation = 1,
                        mediaGrantRevision = 1,
                        visibleChatId = CHAT_ID,
                        chatContextRevision = 1,
                        appliedChatContextRevision = 1,
                        chatContextSynced = true,
                        state = "ended",
                        speechMuted = false,
                        microphoneEnabled = false,
                        foregroundActive = false,
                        reason = "idle_expired",
                        message = null,
                        occurredAt = "2026-07-31T12:05:00Z",
                    ),
                ),
            )

            assertEquals("ended", fixture.controller.state.value.phase)
            assertEquals("idle_expired", fixture.controller.state.value.reason)
            assertFalse(fixture.controller.state.value.mediaConnected)
            assertTrue(fixture.media.disconnectCalls >= 1)
        }

    @Test
    fun foregroundLeaseRenewsEveryTwentySecondsWithoutChangingTrueIdle() =
        runTest {
            val fixture = fixture(this)
            fixture.controller.activate(capability())
            runCurrent()

            advanceTimeBy(19_999)
            runCurrent()
            assertTrue(fixture.api.updateCalls.isEmpty())

            advanceTimeBy(1)
            runCurrent()
            val heartbeat = fixture.api.updateCalls.single()
            assertEquals(DEVICE_ID, heartbeat.binding.deviceId)
            assertEquals(CONNECTION_ID, heartbeat.binding.connectionGeneration)
            assertEquals(BINDING_VALUE, heartbeat.binding.control.binding)
            assertEquals(1, heartbeat.session.generation)
            assertEquals(1, heartbeat.session.mediaGrantRevision)
            assertEquals(true, heartbeat.fields["foreground_active"]?.jsonPrimitive?.booleanOrNull)
            assertEquals("foreground", heartbeat.fields["foreground_reason"]?.jsonPrimitive?.contentOrNull)
            assertFalse("interaction" in heartbeat.fields)

            fixture.controller.appForegroundChanged(active = false, reason = "backgrounded")
            runCurrent()
            val background = fixture.api.updateCalls.last().fields
            assertEquals(false, background["foreground_active"]?.jsonPrimitive?.booleanOrNull)
            assertEquals("backgrounded", background["foreground_reason"]?.jsonPrimitive?.contentOrNull)
            assertEquals(false, background["microphone_enabled"]?.jsonPrimitive?.booleanOrNull)
            assertEquals(1, fixture.media.interruptCalls)
            val foregroundUpdates =
                fixture.api.updateCalls.count {
                    it.fields["foreground_active"]?.jsonPrimitive?.booleanOrNull == true
                }

            advanceTimeBy(60_000)
            runCurrent()
            assertEquals(
                foregroundUpdates,
                fixture.api.updateCalls.count {
                    it.fields["foreground_active"]?.jsonPrimitive?.booleanOrNull == true
                },
            )

            fixture.controller.appForegroundChanged(active = true)
            runCurrent()
            assertEquals("foreground", fixture.api.updateCalls.last().fields["foreground_reason"]?.jsonPrimitive?.contentOrNull)
            advanceTimeBy(20_000)
            runCurrent()
            assertEquals(foregroundUpdates + 2, fixture.api.updateCalls.count {
                it.fields["foreground_active"]?.jsonPrimitive?.booleanOrNull == true
            })

            fixture.controller.end()
            val callsAfterEnd = fixture.api.updateCalls.size
            advanceTimeBy(40_000)
            runCurrent()
            assertEquals(callsAfterEnd, fixture.api.updateCalls.size)
        }

    @Test
    fun finalTranscriptRetriesSameIdsUntilExactCurrentConnectionAcknowledgement() =
        runTest {
            val fixture = fixture(this)
            val submissions = mutableListOf<Pair<String, String>>()
            fixture.controller.setTranscriptSubmitter { transcript, connection ->
                submissions += transcript.submissionId to connection
                true
            }
            fixture.controller.activate(capability())
            fixture.media.emit(
                VoiceMediaEvent.Data(
                    VOICE_TRANSCRIPT_TOPIC,
                    WORKER,
                    transcript(final = false, sequence = 0).encodeToByteArray(),
                ),
            )
            fixture.media.emit(
                VoiceMediaEvent.Data(
                    VOICE_TRANSCRIPT_TOPIC,
                    WORKER,
                    transcript(final = true, sequence = 1).encodeToByteArray(),
                ),
            )
            runCurrent()

            assertEquals("Heard: check my schedule", fixture.controller.state.value.transcriptPreview)
            assertEquals(1, fixture.controller.state.value.awaitingAcceptance)
            assertEquals(listOf(SUBMISSION_ID to CONNECTION_ID), submissions)

            advanceTimeBy(2_500)
            runCurrent()
            assertEquals(2, submissions.size)
            assertEquals(submissions[0], submissions[1])

            fixture.controller.handleInbound(
                Inbound.UserMessageAcked(
                    chatId = CHAT_ID,
                    messageId = "42",
                    submissionId = SUBMISSION_ID,
                    requestGeneration = REQUEST_ID,
                    connectionGeneration = OTHER_CONNECTION_ID,
                    voiceTurnId = TURN_ID,
                ),
            )
            assertEquals(1, fixture.controller.state.value.awaitingAcceptance)

            fixture.controller.handleInbound(
                Inbound.UserMessageAcked(
                    chatId = CHAT_ID,
                    messageId = "42",
                    submissionId = SUBMISSION_ID,
                    requestGeneration = REQUEST_ID,
                    connectionGeneration = CONNECTION_ID,
                    voiceTurnId = TURN_ID,
                ),
            )
            assertEquals(0, fixture.controller.state.value.awaitingAcceptance)
            assertEquals("processing", fixture.controller.state.value.phase)
            advanceTimeBy(5_000)
            runCurrent()
            assertEquals(2, submissions.size)
        }

    @Test
    fun terminalRequestOutcomesExplainWhetherWorkDidNotCompleteOrStart() =
        runTest {
            val expectations =
                listOf(
                    Triple(
                        "failed",
                        VoiceTerminalNoticeKind.REQUEST_DID_NOT_COMPLETE,
                        "Voice request didn't complete",
                    ),
                    Triple(
                        "cancelled",
                        VoiceTerminalNoticeKind.REQUEST_DID_NOT_COMPLETE,
                        "Voice request didn't complete — cancelled",
                    ),
                    Triple(
                        "abandoned",
                        VoiceTerminalNoticeKind.REQUEST_DID_NOT_COMPLETE,
                        "Voice request didn't complete",
                    ),
                    Triple(
                        "refused",
                        VoiceTerminalNoticeKind.REQUEST_DID_NOT_START,
                        "Voice request didn't start",
                    ),
                )

            expectations.forEachIndexed { index, (turnState, expectedKind, expectedTitle) ->
                val fixture = fixture(this)
                fixture.controller.activate(capability())
                val safeServerMessage = "Safe server detail for $turnState."

                fixture.controller.handleInbound(
                    Inbound.VoiceTurnStateFrame(
                        turnState(
                            state = turnState,
                            message = safeServerMessage,
                            sequence = index + 1,
                        ),
                    ),
                )

                val notice = requireNotNull(fixture.controller.state.value.terminalNotice)
                assertEquals(expectedKind, notice.kind)
                assertEquals(expectedTitle, notice.title)
                assertEquals(safeServerMessage, notice.serverMessage)
                assertEquals("Typed chat is still available.", notice.guidance)
                assertTrue(notice.isRequestFailure)
                assertEquals("listening", fixture.controller.state.value.phase)
                assertEquals("voice_turn_$turnState", fixture.controller.state.value.reason)
            }
        }

    @Test
    fun succeededTurnTreatsSpeechFailureAsTextAvailableNotRequestFailure() =
        runTest {
            val fixture = fixture(this)
            fixture.controller.activate(capability())
            val safeServerMessage =
                "Request completed, but spoken playback was unavailable."

            fixture.controller.handleInbound(
                Inbound.VoiceTurnStateFrame(
                    turnState(
                        state = "succeeded",
                        message = safeServerMessage,
                    ),
                ),
            )

            val notice = requireNotNull(fixture.controller.state.value.terminalNotice)
            assertEquals(VoiceTerminalNoticeKind.TEXT_RESULT_AVAILABLE, notice.kind)
            assertEquals("Text result is available", notice.title)
            assertEquals(safeServerMessage, notice.serverMessage)
            assertEquals("2026-07-31T12:00:00Z", notice.occurredAt.toString())
            assertTrue(notice.guidance.contains("text result remains available"))
            assertFalse(notice.isRequestFailure)
            assertFalse(notice.title.contains("fail", ignoreCase = true))
            assertEquals("speaking_result", fixture.controller.state.value.phase)
        }

    @Test
    fun failedSourceOutcomeCreatesExactTurnSpeechNoticeWithoutReplacingNewerTurn() =
        runTest {
            val fixture = fixture(this)
            fixture.controller.activate(capability())
            fixture.controller.handleInbound(
                Inbound.VoiceTurnStateFrame(
                    turnState(
                        state = "succeeded",
                        message = "The result audio could not be delivered.",
                        occurredAt = "2026-07-31T12:05:00Z",
                        speechOutcome = VoiceSpeechOutcome.FAILED,
                    ),
                ),
            )

            val failedSpeech = requireNotNull(fixture.controller.state.value.terminalNotice)
            assertEquals(VoiceTerminalNoticeKind.TEXT_RESULT_AVAILABLE, failedSpeech.kind)
            assertEquals("Speech playback failed", failedSpeech.title)
            assertEquals(TURN_ID, failedSpeech.turnId)
            assertTrue(failedSpeech.speechUnavailable)
            assertTrue(failedSpeech.guidance.contains("text result remains available"))
            assertFalse(failedSpeech.isRequestFailure)
            assertEquals("error", fixture.controller.state.value.phase)
            assertEquals("speech_error", fixture.controller.state.value.reason)

            fixture.controller.handleInbound(
                Inbound.VoiceTurnStateFrame(
                    turnState(
                        state = "succeeded",
                        message = "The newer result completed.",
                        turnId = OTHER_TURN_ID,
                        occurredAt = "2026-07-31T12:05:01Z",
                        speechOutcome = VoiceSpeechOutcome.SOURCE_FINISHED,
                    ),
                ),
            )
            val newer = requireNotNull(fixture.controller.state.value.terminalNotice)
            assertEquals(OTHER_TURN_ID, newer.turnId)
            assertFalse(newer.speechUnavailable)

            fixture.controller.handleInbound(
                Inbound.VoiceTurnStateFrame(
                    turnState(
                        state = "succeeded",
                        message = "This older audio failure must stay hidden.",
                        occurredAt = "2026-07-31T12:05:00Z",
                        sequence = 2,
                        speechOutcome = VoiceSpeechOutcome.FAILED,
                    ),
                ),
            )
            assertEquals(newer, fixture.controller.state.value.terminalNotice)
            assertEquals("speaking_result", fixture.controller.state.value.phase)
            assertEquals("ready", fixture.controller.state.value.reason)
        }

    @Test
    fun nonFailureSpeechOutcomesRemainNormalSuccessfulResults() =
        runTest {
            listOf(
                null,
                VoiceSpeechOutcome.SOURCE_FINISHED,
                VoiceSpeechOutcome.SUPPRESSED,
            ).forEach { outcome ->
                val fixture = fixture(this)
                fixture.controller.activate(capability())
                fixture.controller.handleInbound(
                    Inbound.VoiceTurnStateFrame(
                        turnState(
                            state = "succeeded",
                            message = "The text result is available.",
                            speechOutcome = outcome,
                        ),
                    ),
                )

                val notice = requireNotNull(fixture.controller.state.value.terminalNotice)
                assertEquals("Text result is available", notice.title)
                assertFalse(notice.speechUnavailable)
                assertEquals("speaking_result", fixture.controller.state.value.phase)
                assertEquals("ready", fixture.controller.state.value.reason)
            }
        }

    @Test
    fun droppedResultSpeechKeepsSuccessfulTextNoticeAndNeverBecomesRequestFailure() =
        runTest {
            val fixture = fixture(this)
            fixture.controller.activate(capability())
            val safeServerMessage = "Request completed. The text result is available in the conversation."
            fixture.controller.handleInbound(
                Inbound.VoiceTurnStateFrame(
                    turnState(
                        state = "succeeded",
                        message = safeServerMessage,
                    ),
                ),
            )
            fixture.media.emit(
                VoiceMediaEvent.Data(
                    VOICE_ANNOUNCEMENT_TOPIC,
                    WORKER,
                    announcement(
                        kind = "result",
                        turnId = TURN_ID,
                        quantumRole = "result_opening",
                    ).encodeToByteArray(),
                ),
            )
            runCurrent()

            fixture.media.emit(VoiceMediaEvent.AnnouncementDropped(ANNOUNCEMENT_ID, 1))
            runCurrent()

            val notice = requireNotNull(fixture.controller.state.value.terminalNotice)
            assertEquals(VoiceTerminalNoticeKind.TEXT_RESULT_AVAILABLE, notice.kind)
            assertEquals("Speech playback failed", notice.title)
            assertEquals(safeServerMessage, notice.serverMessage)
            assertEquals("2026-07-31T12:00:00Z", notice.occurredAt.toString())
            assertTrue(notice.speechUnavailable)
            assertFalse(notice.isRequestFailure)
            assertEquals("speech_error", fixture.controller.state.value.reason)
        }

    @Test
    fun localPlayoutFailureSurvivesSameTurnSourceFinishedAndAbsorbsServerDetail() =
        runTest {
            val fixture = fixture(this)
            fixture.controller.activate(capability())
            fixture.controller.handleInbound(
                Inbound.VoiceTurnStateFrame(
                    turnState(
                        state = "processing",
                        message = "The request is still running.",
                    ),
                ),
            )
            fixture.media.emit(
                VoiceMediaEvent.Data(
                    VOICE_ANNOUNCEMENT_TOPIC,
                    WORKER,
                    announcement(
                        kind = "result",
                        turnId = TURN_ID,
                        quantumRole = "result_opening",
                    ).encodeToByteArray(),
                ),
            )
            runCurrent()
            fixture.media.emit(VoiceMediaEvent.AnnouncementDropped(ANNOUNCEMENT_ID, 1))
            runCurrent()

            val localFailure = requireNotNull(fixture.controller.state.value.terminalNotice)
            assertEquals(TURN_ID, localFailure.turnId)
            assertEquals("Speech playback failed", localFailure.title)
            assertTrue(localFailure.speechUnavailable)
            assertEquals(null, localFailure.serverMessage)

            val committedMessage = "Request completed. The text result is available in the conversation."
            fixture.controller.handleInbound(
                Inbound.VoiceTurnStateFrame(
                    turnState(
                        state = "succeeded",
                        message = committedMessage,
                        sequence = 2,
                        speechOutcome = VoiceSpeechOutcome.SOURCE_FINISHED,
                    ),
                ),
            )

            val preserved = requireNotNull(fixture.controller.state.value.terminalNotice)
            assertEquals(TURN_ID, preserved.turnId)
            assertEquals("Speech playback failed", preserved.title)
            assertEquals(committedMessage, preserved.serverMessage)
            assertTrue(preserved.guidance.contains("committed text result remains available"))
            assertTrue(preserved.speechUnavailable)
            assertFalse(preserved.isRequestFailure)
            assertEquals("error", fixture.controller.state.value.phase)
            assertEquals("speech_error", fixture.controller.state.value.reason)
        }

    @Test
    fun staleDroppedManifestCannotRelabelANewerTurnNotice() =
        runTest {
            val fixture = fixture(this)
            fixture.controller.activate(capability())
            fixture.controller.handleInbound(
                Inbound.VoiceTurnStateFrame(
                    turnState(state = "processing", message = "The older request is running."),
                ),
            )
            fixture.media.emit(
                VoiceMediaEvent.Data(
                    VOICE_ANNOUNCEMENT_TOPIC,
                    WORKER,
                    announcement(
                        kind = "result",
                        turnId = TURN_ID,
                        quantumRole = "result_opening",
                    ).encodeToByteArray(),
                ),
            )
            runCurrent()
            fixture.controller.handleInbound(
                Inbound.VoiceTurnStateFrame(
                    turnState(
                        state = "succeeded",
                        message = "The newer result completed.",
                        turnId = OTHER_TURN_ID,
                        occurredAt = "2026-07-31T12:05:01Z",
                        speechOutcome = VoiceSpeechOutcome.SOURCE_FINISHED,
                    ),
                ),
            )
            val newer = requireNotNull(fixture.controller.state.value.terminalNotice)

            fixture.media.emit(VoiceMediaEvent.AnnouncementDropped(ANNOUNCEMENT_ID, 1))
            runCurrent()

            assertEquals(newer, fixture.controller.state.value.terminalNotice)
            assertFalse(requireNotNull(fixture.controller.state.value.terminalNotice).speechUnavailable)
            assertEquals("speaking_result", fixture.controller.state.value.phase)
            assertEquals("ready", fixture.controller.state.value.reason)
        }

    @Test
    fun onlyDifferentActiveTurnClearsTerminalNoticeAndStaleGenerationCannotCreateOne() =
        runTest {
            val fixture = fixture(this)
            fixture.controller.activate(capability())
            fixture.controller.handleInbound(
                Inbound.VoiceTurnStateFrame(
                    turnState(
                        state = "failed",
                        message = "The request did not complete.",
                    ),
                ),
            )
            assertTrue(fixture.controller.state.value.terminalNotice != null)

            fixture.controller.handleInbound(
                Inbound.VoiceTurnStateFrame(
                    turnState(
                        state = "processing",
                        message = "Late same-turn processing state.",
                        sequence = 2,
                    ),
                ),
            )
            assertTrue(fixture.controller.state.value.terminalNotice != null)
            assertEquals("listening", fixture.controller.state.value.phase)

            fixture.controller.handleInbound(
                Inbound.VoiceTurnStateFrame(
                    turnState(
                        state = "processing",
                        message = "A newer request is working.",
                        turnId = OTHER_TURN_ID,
                        sequence = 1,
                    ),
                ),
            )
            assertEquals(null, fixture.controller.state.value.terminalNotice)
            assertEquals("processing", fixture.controller.state.value.phase)

            fixture.controller.handleInbound(
                Inbound.VoiceTurnStateFrame(
                    turnState(
                        state = "refused",
                        message = "The request did not start.",
                        generation = 2,
                        sequence = 3,
                    ),
                ),
            )
            assertEquals(null, fixture.controller.state.value.terminalNotice)
            assertEquals("processing", fixture.controller.state.value.phase)
        }

    @Test
    fun onlyNonOlderDifferentTurnCanClearOrReplaceTerminalNotice() =
        runTest {
            val terminalAt = "2026-07-31T12:05:00Z"
            val olderAt = "2026-07-31T12:04:59Z"
            val newerAt = "2026-07-31T12:05:01Z"

            for (state in listOf("accepted", "processing", "succeeded")) {
                val fixture = fixture(this)
                fixture.controller.activate(capability())
                fixture.controller.handleInbound(
                    Inbound.VoiceTurnStateFrame(
                        turnState(
                            state = "failed",
                            message = "The newer request did not complete.",
                            occurredAt = terminalAt,
                        ),
                    ),
                )
                val terminal = requireNotNull(fixture.controller.state.value.terminalNotice)

                fixture.controller.handleInbound(
                    Inbound.VoiceTurnStateFrame(
                        turnState(
                            state = state,
                            message = "This older turn must not supersede the notice.",
                            turnId = OTHER_TURN_ID,
                            occurredAt = olderAt,
                        ),
                    ),
                )
                assertEquals(
                    terminal,
                    fixture.controller.state.value.terminalNotice,
                    "an older different-turn $state frame must retain the newer terminal notice",
                )

                fixture.controller.handleInbound(
                    Inbound.VoiceTurnStateFrame(
                        turnState(
                            state = state,
                            message = "This newer turn may supersede the notice.",
                            turnId = OTHER_TURN_ID,
                            sequence = 2,
                            occurredAt = newerAt,
                        ),
                    ),
                )
                if (state == "succeeded") {
                    val replacement = requireNotNull(fixture.controller.state.value.terminalNotice)
                    assertEquals(VoiceTerminalNoticeKind.TEXT_RESULT_AVAILABLE, replacement.kind)
                    assertEquals(OTHER_TURN_ID, replacement.turnId)
                } else {
                    assertEquals(null, fixture.controller.state.value.terminalNotice)
                }
            }
        }

    @Test
    fun olderDifferentTurnRejectionCannotReplaceNewerTerminalNotice() {
        val current =
            requireNotNull(
                terminalNoticeFor(
                    turnState(
                        state = "failed",
                        message = "Keep this newer failure.",
                        occurredAt = "2026-07-31T12:05:00Z",
                    ),
                ),
            )
        val olderRejection =
            VoiceSubmissionRejected(
                sessionId = SESSION_ID,
                connectionGeneration = CONNECTION_ID,
                generation = 1,
                mediaGrantRevision = 1,
                turnId = OTHER_TURN_ID,
                clientTurnId = CLIENT_TURN_ID,
                submissionId = SUBMISSION_ID,
                requestGeneration = REQUEST_ID,
                chatId = CHAT_ID,
                reason = "capacity_exhausted",
                retryPolicy = "explicit_user_retry",
                message = "This older rejection must stay hidden.",
                occurredAt = "2026-07-31T12:04:59Z",
            )

        assertEquals(current, reduceTerminalNotice(current, olderRejection))

        val replacement =
            reduceTerminalNotice(
                current,
                olderRejection.copy(
                    message = "This newer rejection may replace the notice.",
                    occurredAt = "2026-07-31T12:05:01Z",
                ),
            )
        assertEquals(VoiceTerminalNoticeKind.REQUEST_DID_NOT_START, replacement.kind)
        assertEquals(OTHER_TURN_ID, replacement.turnId)
    }

    @Test
    fun transcriptAndAcknowledgementCannotClearNoticeWithoutTimestampedNewerTurn() =
        runTest {
            val fixture = fixture(this)
            fixture.controller.setTranscriptSubmitter { _, _ -> true }
            fixture.controller.activate(capability())
            fixture.media.emit(
                VoiceMediaEvent.Data(
                    VOICE_TRANSCRIPT_TOPIC,
                    WORKER,
                    transcript(final = true, sequence = 1).encodeToByteArray(),
                ),
            )
            runCurrent()

            fixture.controller.handleInbound(
                Inbound.VoiceTurnStateFrame(
                    turnState(
                        state = "failed",
                        message = "Keep this newer terminal notice.",
                        turnId = OTHER_TURN_ID,
                        occurredAt = "2026-07-31T12:05:00Z",
                    ),
                ),
            )
            val terminal = requireNotNull(fixture.controller.state.value.terminalNotice)

            fixture.media.emit(
                VoiceMediaEvent.Data(
                    VOICE_TRANSCRIPT_TOPIC,
                    WORKER,
                    transcript(final = false, sequence = 2).encodeToByteArray(),
                ),
            )
            runCurrent()
            assertEquals(terminal, fixture.controller.state.value.terminalNotice)

            fixture.controller.handleInbound(
                Inbound.UserMessageAcked(
                    chatId = CHAT_ID,
                    messageId = "42",
                    submissionId = SUBMISSION_ID,
                    requestGeneration = REQUEST_ID,
                    connectionGeneration = CONNECTION_ID,
                    voiceTurnId = TURN_ID,
                ),
            )
            assertEquals(terminal, fixture.controller.state.value.terminalNotice)

            fixture.controller.handleInbound(
                Inbound.VoiceTurnStateFrame(
                    turnState(
                        state = "accepted",
                        message = "The newer turn was accepted.",
                        occurredAt = "2026-07-31T12:05:01Z",
                    ),
                ),
            )
            assertEquals(null, fixture.controller.state.value.terminalNotice)
        }

    @Test
    fun unrelatedSessionStateKeepsTerminalNoticeButExplicitEndClearsIt() =
        runTest {
            val fixture = fixture(this)
            fixture.controller.activate(capability())
            fixture.controller.handleInbound(
                Inbound.VoiceTurnStateFrame(
                    turnState(
                        state = "failed",
                        message = "The request did not complete.",
                    ),
                ),
            )

            fixture.controller.handleInbound(
                Inbound.VoiceSessionStateFrame(
                    sessionState(
                        chatContextSynced = true,
                        microphoneEnabled = true,
                        state = "active",
                    ),
                ),
            )
            assertTrue(fixture.controller.state.value.terminalNotice != null)

            fixture.controller.end()
            assertEquals(null, fixture.controller.state.value.terminalNotice)
            assertEquals("ended", fixture.controller.state.value.phase)
        }

    @Test
    fun successfulDifferentTurnReplacesFailureAndConnectionResetClearsTheNotice() =
        runTest {
            val fixture = fixture(this)
            fixture.controller.activate(capability())
            fixture.controller.handleInbound(
                Inbound.VoiceTurnStateFrame(
                    turnState(
                        state = "failed",
                        message = "The first request did not complete.",
                    ),
                ),
            )

            fixture.controller.handleInbound(
                Inbound.VoiceTurnStateFrame(
                    turnState(
                        state = "succeeded",
                        message = "The next request completed.",
                        turnId = OTHER_TURN_ID,
                    ),
                ),
            )

            val succeeded = requireNotNull(fixture.controller.state.value.terminalNotice)
            assertEquals(VoiceTerminalNoticeKind.TEXT_RESULT_AVAILABLE, succeeded.kind)
            assertEquals(OTHER_TURN_ID, succeeded.turnId)
            assertFalse(succeeded.isRequestFailure)

            fixture.controller.installUiConnection("user-token", DEVICE_ID, OTHER_CONNECTION_ID, CHAT_ID)
            assertEquals(null, fixture.controller.state.value.terminalNotice)
        }

    @Test
    fun correlatedRejectionClearsFinalAndRequiresExplicitFreshSpeechRetry() =
        runTest {
            val fixture = fixture(this)
            var submissions = 0
            fixture.controller.setTranscriptSubmitter { _, _ ->
                submissions += 1
                true
            }
            fixture.controller.activate(capability())
            fixture.media.emit(
                VoiceMediaEvent.Data(
                    VOICE_TRANSCRIPT_TOPIC,
                    WORKER,
                    transcript(final = true, sequence = 1).encodeToByteArray(),
                ),
            )
            runCurrent()
            assertEquals(1, fixture.controller.state.value.awaitingAcceptance)
            assertEquals(1, submissions)

            val safeServerMessage = "The chat is no longer available."

            fixture.controller.handleInbound(
                Inbound.VoiceSubmissionRejectedFrame(
                    VoiceSubmissionRejected(
                        sessionId = SESSION_ID,
                        connectionGeneration = CONNECTION_ID,
                        generation = 1,
                        mediaGrantRevision = 1,
                        turnId = TURN_ID,
                        clientTurnId = CLIENT_TURN_ID,
                        submissionId = SUBMISSION_ID,
                        requestGeneration = REQUEST_ID,
                        chatId = CHAT_ID,
                        reason = "chat_unavailable",
                        retryPolicy = "explicit_user_retry",
                        message = safeServerMessage,
                        occurredAt = "2026-07-31T12:00:00Z",
                    ),
                ),
            )

            assertEquals(0, fixture.controller.state.value.awaitingAcceptance)
            assertEquals("listening", fixture.controller.state.value.phase)
            assertEquals("voice_submission_rejected", fixture.controller.state.value.reason)
            assertEquals(safeServerMessage, fixture.controller.state.value.message)
            val notice = requireNotNull(fixture.controller.state.value.terminalNotice)
            assertEquals(VoiceTerminalNoticeKind.REQUEST_DID_NOT_START, notice.kind)
            assertEquals("Voice request didn't start", notice.title)
            assertEquals(safeServerMessage, notice.serverMessage)
            assertEquals("explicit_user_retry", notice.retryPolicy)
            assertEquals(
                "Retry requires a new explicit spoken request. Typed chat is still available.",
                notice.guidance,
            )

            advanceTimeBy(5_000)
            runCurrent()
            assertEquals(1, submissions)
        }

    @Test
    fun mismatchedSubmissionRejectionCannotClearOrTerminalizeThePendingTurn() =
        runTest {
            val fixture = fixture(this)
            var submissions = 0
            fixture.controller.setTranscriptSubmitter { _, _ ->
                submissions += 1
                true
            }
            fixture.controller.activate(capability())
            fixture.media.emit(
                VoiceMediaEvent.Data(
                    VOICE_TRANSCRIPT_TOPIC,
                    WORKER,
                    transcript(final = true, sequence = 1).encodeToByteArray(),
                ),
            )
            runCurrent()

            fixture.controller.handleInbound(
                Inbound.VoiceSubmissionRejectedFrame(
                    VoiceSubmissionRejected(
                        sessionId = SESSION_ID,
                        connectionGeneration = CONNECTION_ID,
                        generation = 1,
                        mediaGrantRevision = 1,
                        turnId = OTHER_TURN_ID,
                        clientTurnId = CLIENT_TURN_ID,
                        submissionId = SUBMISSION_ID,
                        requestGeneration = REQUEST_ID,
                        chatId = CHAT_ID,
                        reason = "chat_unavailable",
                        retryPolicy = "explicit_user_retry",
                        message = "This stale rejection must be ignored.",
                        occurredAt = "2026-07-31T12:00:00Z",
                    ),
                ),
            )

            assertEquals(1, fixture.controller.state.value.awaitingAcceptance)
            assertEquals(null, fixture.controller.state.value.terminalNotice)
            advanceTimeBy(2_500)
            runCurrent()
            assertEquals(2, submissions)
        }

    @Test
    fun suspensionStopsMediaButRetainsTheBoundFinalForCurrentSocketRetry() =
        runTest {
            val fixture = fixture(this)
            fixture.controller.setTranscriptSubmitter { _, _ -> true }
            fixture.controller.activate(capability())
            fixture.media.emit(
                VoiceMediaEvent.Data(
                    VOICE_TRANSCRIPT_TOPIC,
                    WORKER,
                    transcript(final = true, sequence = 1).encodeToByteArray(),
                ),
            )
            runCurrent()

            fixture.controller.handleInbound(
                Inbound.VoiceSessionStateFrame(
                    VoiceSessionState(
                        SESSION_ID,
                        CONNECTION_ID,
                        1,
                        1,
                        CHAT_ID,
                        1,
                        1,
                        true,
                        "suspended",
                        false,
                        false,
                        false,
                        "backgrounded",
                        null,
                        "2026-07-31T12:00:00Z",
                    ),
                ),
            )

            assertEquals("suspended", fixture.controller.state.value.phase)
            assertEquals(1, fixture.controller.state.value.awaitingAcceptance)
            assertFalse(fixture.controller.state.value.mediaConnected)
        }

    private fun fixture(scope: TestScope): Fixture {
        val api = FakeApi()
        val media = FakeMedia()
        val controller =
            VoiceSessionController(
                api = api,
                media = media,
                scope = scope.backgroundScope,
                uuidFactory = { ACTIVATION_ID },
                clock = { java.time.Instant.parse("2026-07-31T12:00:00Z") },
            )
        controller.installUiConnection("user-token", DEVICE_ID, CONNECTION_ID, CHAT_ID)
        controller.consumeBinding(
            VoiceControlBinding(
                DEVICE_ID,
                CONNECTION_ID,
                BINDING_ID,
                BINDING_VALUE,
                "2026-07-31T12:10:00Z",
            ),
        )
        return Fixture(controller, api, media)
    }

    private data class Fixture(
        val controller: VoiceSessionController,
        val api: FakeApi,
        val media: FakeMedia,
    )

    private class FakeApi : VoiceControlApi {
        data class UpdateCall(
            val binding: VoiceUiBinding,
            val session: VoiceRestSession,
            val fields: JsonObject,
        )

        var startOutcome: VoiceStartOutcome = VoiceStartOutcome.Started(session(), grant())
        var takeoverOutcome: VoiceStartOutcome = VoiceStartOutcome.Started(session(), grant())
        var startCalls = 0
        var takeoverCalls = 0
        var stopCalls = 0
        var endCalls = 0
        var updatedSession: VoiceRestSession? = null
        val updateCalls = mutableListOf<UpdateCall>()
        var updateStarted: CompletableDeferred<Unit>? = null
        var releaseUpdate: CompletableDeferred<Unit>? = null

        override suspend fun start(
            binding: VoiceUiBinding,
            activationId: String,
            capability: VoiceMediaCapability,
        ): VoiceStartOutcome {
            startCalls += 1
            return startOutcome
        }

        override suspend fun takeover(
            binding: VoiceUiBinding,
            activationId: String,
            target: VoiceTakeoverTarget,
            capability: VoiceMediaCapability,
        ): VoiceStartOutcome {
            takeoverCalls += 1
            return takeoverOutcome
        }

        override suspend fun update(
            binding: VoiceUiBinding,
            session: VoiceRestSession,
            fields: JsonObject,
        ): Result<VoiceRestSession> {
            updateCalls += UpdateCall(binding, session, fields)
            updateStarted?.complete(Unit)
            releaseUpdate?.await()
            val updated =
                session.copy(
                    microphoneEnabled =
                        fields["microphone_enabled"]?.jsonPrimitive?.booleanOrNull ?: session.microphoneEnabled,
                    speechMuted = fields["speech_muted"]?.jsonPrimitive?.booleanOrNull ?: session.speechMuted,
                    visibleChatId = fields["visible_chat_id"]?.jsonPrimitive?.contentOrNull ?: session.visibleChatId,
                    foregroundActive =
                        fields["foreground_active"]?.jsonPrimitive?.booleanOrNull ?: session.foregroundActive,
                )
            updatedSession = updated
            return Result.success(updated)
        }

        override suspend fun stopSpeech(
            binding: VoiceUiBinding,
            session: VoiceRestSession,
        ): Boolean {
            stopCalls += 1
            return true
        }

        override suspend fun end(
            binding: VoiceUiBinding,
            session: VoiceRestSession,
        ): Boolean {
            endCalls += 1
            return true
        }
    }

    private class FakeMedia : VoiceMediaClient {
        private val flow = MutableSharedFlow<VoiceMediaEvent>(replay = 16, extraBufferCapacity = 16)
        override val events: Flow<VoiceMediaEvent> = flow
        var connectCalls = 0
        var disconnectCalls = 0
        var connectError: Exception? = null
        var connectStarted: CompletableDeferred<Unit>? = null
        var releaseConnect: CompletableDeferred<Unit>? = null
        val microphoneChanges = mutableListOf<Boolean>()
        val announcements = mutableListOf<com.personalailabs.astraldeep.core.protocol.VoiceAnnouncementMedia>()
        var acceptAnnouncements = true
        var interruptCalls = 0

        override suspend fun connect(grant: LiveKitVoiceGrant) {
            connectCalls += 1
            connectStarted?.complete(Unit)
            releaseConnect?.await()
            connectError?.let { throw it }
        }

        override suspend fun setMicrophoneEnabled(enabled: Boolean) {
            microphoneChanges += enabled
        }

        override suspend fun queueAnnouncement(value: com.personalailabs.astraldeep.core.protocol.VoiceAnnouncementMedia): Boolean {
            if (acceptAnnouncements) announcements += value
            return acceptAnnouncements
        }

        override fun interruptPlayout() {
            interruptCalls += 1
        }

        override fun disconnect() {
            disconnectCalls += 1
        }

        fun emit(event: VoiceMediaEvent) {
            assertTrue(flow.tryEmit(event))
        }
    }

    companion object {
        private const val DEVICE_ID = "00000000-0000-4000-8000-000000000001"
        private const val CONNECTION_ID = "00000000-0000-4000-8000-000000000002"
        private const val OTHER_CONNECTION_ID = "00000000-0000-4000-8000-000000000012"
        private const val SESSION_ID = "00000000-0000-4000-8000-000000000003"
        private const val CHAT_ID = "00000000-0000-4000-8000-000000000004"
        private const val OTHER_CHAT_ID = "00000000-0000-4000-8000-000000000014"
        private const val TURN_ID = "00000000-0000-4000-8000-000000000005"
        private const val OTHER_TURN_ID = "00000000-0000-4000-8000-000000000015"
        private const val CLIENT_TURN_ID = "00000000-0000-4000-8000-000000000006"
        private const val SUBMISSION_ID = "00000000-0000-4000-8000-000000000007"
        private const val REQUEST_ID = "00000000-0000-4000-8000-000000000008"
        private const val ANNOUNCEMENT_ID = "00000000-0000-4000-8000-000000000009"
        private const val BINDING_ID = "00000000-0000-4000-8000-00000000000a"
        private const val BINDING_VALUE = "synthetic-binding-value-000000000000"
        private const val ACTIVATION_ID = "00000000-0000-4000-8000-00000000000b"
        private const val WORKER = "voice-worker-a"

        private fun capability(
            hasMicrophone: Boolean = true,
            hasAudioOutput: Boolean = true,
            permission: String = "authorized",
        ) = VoiceMediaCapability(hasMicrophone, hasAudioOutput, permission, true)

        private fun composer(
            state: String,
            foregroundActive: Boolean = false,
            microphoneEnabled: Boolean = false,
        ) =
            VoiceComposerModel(
                available = true,
                state = state,
                speechMuted = false,
                microphoneEnabled = microphoneEnabled,
                foregroundActive = foregroundActive,
                reason = "ready",
                outputLocale = "en-US",
                message = null,
                chatContextRevision = 1.takeIf { foregroundActive },
                appliedChatContextRevision = 1.takeIf { foregroundActive },
                chatContextSynced = foregroundActive,
                sessionId = SESSION_ID.takeIf { foregroundActive },
                generation = 1.takeIf { foregroundActive },
                mediaGrantRevision = 1.takeIf { foregroundActive },
                visibleChatId = CHAT_ID,
                foregroundTurnId = null,
                ownerDevice =
                    VoiceOwnerDevice(DEVICE_ID, "android", null, 1)
                        .takeIf { foregroundActive },
                idleExpiresAt = null,
                controls =
                    listOf(
                        VoiceControl(
                            key = if (foregroundActive) "voice-end" else "voice-start",
                            action = if (foregroundActive) "voice_session_end" else "voice_session_start",
                            label = if (foregroundActive) "End voice conversation" else "Start voice conversation",
                            icon = if (foregroundActive) "stop" else "microphone",
                            visible = true,
                            enabled = true,
                            pressed = false,
                            busy = false,
                        ),
                    ),
            )

        private fun session() =
            VoiceRestSession(
                sessionId = SESSION_ID,
                deviceId = DEVICE_ID,
                ownerConnectionGeneration = CONNECTION_ID,
                visibleChatId = CHAT_ID,
                appliedVisibleChatId = CHAT_ID,
                generation = 1,
                mediaGrantRevision = 1,
                chatContextRevision = 1,
                appliedChatContextRevision = 1,
                chatContextSynced = true,
                state = "active",
                foregroundActive = true,
                speechMuted = false,
                microphoneEnabled = true,
            )

        private fun sessionState(
            chatContextSynced: Boolean,
            microphoneEnabled: Boolean,
            state: String,
        ) =
            VoiceSessionState(
                sessionId = SESSION_ID,
                connectionGeneration = CONNECTION_ID,
                generation = 1,
                mediaGrantRevision = 1,
                visibleChatId = CHAT_ID,
                chatContextRevision = 1,
                appliedChatContextRevision = 1.takeIf { chatContextSynced },
                chatContextSynced = chatContextSynced,
                state = state,
                speechMuted = false,
                microphoneEnabled = microphoneEnabled,
                foregroundActive = true,
                reason = "ready",
                message = null,
                occurredAt = "2026-07-31T12:00:00Z",
            )

        private fun turnState(
            state: String,
            message: String?,
            generation: Int = 1,
            sequence: Int = 1,
            turnId: String = TURN_ID,
            occurredAt: String = "2026-07-31T12:00:00Z",
            speechOutcome: VoiceSpeechOutcome? = null,
        ) =
            VoiceTurnState(
                sessionId = SESSION_ID,
                connectionGeneration = CONNECTION_ID,
                generation = generation,
                mediaGrantRevision = 1,
                turnId = turnId,
                clientTurnId = CLIENT_TURN_ID,
                submissionId = SUBMISSION_ID,
                requestGeneration = REQUEST_ID,
                chatId = CHAT_ID,
                chatContextRevision = 1,
                detectedLanguage = "en",
                spokenOutputPolicy = "full_recap",
                outputReason = "ready",
                state = state,
                foreground = true,
                sensitiveResultPending = false,
                sequence = sequence,
                speechOutcome = speechOutcome,
                resultId = "result-1".takeIf { state == "succeeded" },
                message = message,
                occurredAt = occurredAt,
            )

        private fun grant() =
            LiveKitVoiceGrant(
                grantId = "grant-a",
                sessionId = SESSION_ID,
                generation = 1,
                mediaGrantRevision = 1,
                expiresAt = "2026-07-31T12:10:00Z",
                url = "wss://voice.example.test",
                joinToken = "synthetic-client-token-000000000000000000000000",
                roomName = "voice-room-a",
                participantIdentity = "voice-client-a",
                workerIdentity = WORKER,
            )

        private fun transcript(
            final: Boolean,
            sequence: Int,
        ): String =
            """
            {
              "type":"voice_transcript",
              "schema_version":"1",
              "session_id":"$SESSION_ID",
              "generation":1,
              "turn_id":"$TURN_ID",
              "client_turn_id":"$CLIENT_TURN_ID",
              "submission_id":"$SUBMISSION_ID",
              "request_generation":"$REQUEST_ID",
              "chat_id":"$CHAT_ID",
              "chat_context_revision":1,
              "media_grant_revision":1,
              "sequence":$sequence,
              "final":$final,
              "text":"check my schedule",
              "detected_language":${if (final) "\"en\"" else "null"},
              ${if (final) "\"text_digest_sha256\":\"${"a".repeat(64)}\",\"transcript_proof\":\"${"b".repeat(64)}\",\"proof_expires_at\":\"2026-07-31T12:10:00Z\"," else ""}
              "source_participant_identity":"$WORKER"
            }
            """.trimIndent()

        private fun announcement(
            kind: String,
            turnId: String?,
            quantumRole: String = "single",
            quantumIndex: Int = 0,
            announcementId: String = ANNOUNCEMENT_ID,
            announcementSequence: Int = 1,
            durationSamples: Int = 12_000,
            reservedSamples: Int = 36_000,
        ): String =
            """
            {
              "type":"voice_announcement_media",
              "schema_version":"1",
              "session_id":"$SESSION_ID",
              "generation":1,
              "media_grant_revision":1,
              "announcement_id":"$announcementId",
              "announcement_sequence":$announcementSequence,
              "turn_id":${turnId?.let { "\"$it\"" } ?: "null"},
              "kind":"$kind",
              "quantum_role":"$quantumRole",
              "quantum_index":$quantumIndex,
              "transport":"livekit",
              "worker_identity":"$WORKER",
              "sample_rate_hz":24000,
              "duration_samples":$durationSamples,
              ${if (quantumRole != "single") "\"result_reserved_samples_after\":$reservedSamples," else ""}
              "track_sid":"TR_audio_1",
              "track_name":"announcement-1"
            }
            """.trimIndent()
    }
}
