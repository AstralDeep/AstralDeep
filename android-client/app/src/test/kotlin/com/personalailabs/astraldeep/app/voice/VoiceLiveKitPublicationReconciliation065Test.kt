package com.personalailabs.astraldeep.app.voice

import kotlin.test.Test
import kotlin.test.assertEquals

class VoiceLiveKitPublicationReconciliation065Test {
    @Test
    fun joinSnapshotWorkerAudioIsRememberedAfterAMissedPublishEvent() {
        assertEquals(
            VoicePublicationDiscovery.REMEMBER,
            voicePublicationDiscovery(
                expectedWorkerIdentity = WORKER,
                participantIdentity = WORKER,
                isAudio = true,
                alreadyRemembered = false,
            ),
        )
    }

    @Test
    fun eventAndSnapshotRediscoveryNeverUnsubscribesTheMatchedTrack() {
        assertEquals(
            VoicePublicationDiscovery.ALREADY_REMEMBERED,
            voicePublicationDiscovery(
                expectedWorkerIdentity = WORKER,
                participantIdentity = WORKER,
                isAudio = true,
                alreadyRemembered = true,
            ),
        )
    }

    @Test
    fun snapshotReconciliationRejectsNonWorkerAndNonAudioPublications() {
        assertEquals(
            VoicePublicationDiscovery.REJECT,
            voicePublicationDiscovery(
                expectedWorkerIdentity = WORKER,
                participantIdentity = "unexpected-worker",
                isAudio = true,
                alreadyRemembered = false,
            ),
        )
        assertEquals(
            VoicePublicationDiscovery.REJECT,
            voicePublicationDiscovery(
                expectedWorkerIdentity = WORKER,
                participantIdentity = WORKER,
                isAudio = false,
                alreadyRemembered = false,
            ),
        )
    }

    private companion object {
        const val WORKER = "voice-worker-a"
    }
}
