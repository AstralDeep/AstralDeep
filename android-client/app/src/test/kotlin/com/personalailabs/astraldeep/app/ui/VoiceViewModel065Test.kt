package com.personalailabs.astraldeep.app.ui

import com.personalailabs.astraldeep.app.transport.LocalSubmission
import com.personalailabs.astraldeep.core.protocol.Inbound
import kotlin.test.Test
import kotlin.test.assertEquals
import kotlin.test.assertFalse
import kotlin.test.assertTrue

class VoiceViewModel065Test {
    @Test
    fun onlyExactCorrelatedNoChatResponseMaySelectTheCreatedVoiceChat() {
        val state = UiState(connectionGeneration = CONNECTION_ID)
        val pending = LocalSubmission("new_chat", null, SUBMISSION_ID, REQUEST_ID)
        val exact =
            Inbound.ChatCreated(
                chatId = CHAT_ID,
                connectionGeneration = CONNECTION_ID,
                submissionId = SUBMISSION_ID,
                requestGeneration = REQUEST_ID,
                fromMessage = false,
            )

        assertTrue(isExpectedVoiceChatCreation(state, pending, exact))
        assertFalse(isExpectedVoiceChatCreation(state.copy(activeChatId = OTHER_CHAT_ID), pending, exact))
        assertFalse(isExpectedVoiceChatCreation(state, pending, exact.copy(connectionGeneration = OTHER_CONNECTION_ID)))
        assertFalse(isExpectedVoiceChatCreation(state, pending, exact.copy(submissionId = OTHER_SUBMISSION_ID)))
        assertFalse(isExpectedVoiceChatCreation(state, pending, exact.copy(fromMessage = true)))
    }

    @Test
    fun onlyAcquisitionControlsRunTheVisibleChatPreflight() {
        val dispositions =
            listOf(
                "voice_session_start",
                "voice_session_takeover",
                "voice_session_end",
                "voice_microphone_set",
                "voice_speech_stop",
                "voice_speech_mute_set",
                "voice_visible_chat_update",
                "voice_sensitive_recap_request",
            ).associateWith(::voiceControlNeedsChatPreflight)

        assertEquals(
            mapOf(
                "voice_session_start" to true,
                "voice_session_takeover" to true,
                "voice_session_end" to false,
                "voice_microphone_set" to false,
                "voice_speech_stop" to false,
                "voice_speech_mute_set" to false,
                "voice_visible_chat_update" to false,
                "voice_sensitive_recap_request" to false,
            ),
            dispositions,
        )
    }

    companion object {
        private const val CONNECTION_ID = "00000000-0000-4000-8000-000000000001"
        private const val OTHER_CONNECTION_ID = "00000000-0000-4000-8000-000000000002"
        private const val CHAT_ID = "00000000-0000-4000-8000-000000000003"
        private const val OTHER_CHAT_ID = "00000000-0000-4000-8000-000000000004"
        private const val SUBMISSION_ID = "00000000-0000-4000-8000-000000000005"
        private const val OTHER_SUBMISSION_ID = "00000000-0000-4000-8000-000000000006"
        private const val REQUEST_ID = "00000000-0000-4000-8000-000000000007"
    }
}
