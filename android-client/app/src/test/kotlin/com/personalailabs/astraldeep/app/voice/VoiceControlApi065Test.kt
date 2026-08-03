package com.personalailabs.astraldeep.app.voice

import com.personalailabs.astraldeep.core.protocol.VoiceControlBinding
import kotlinx.coroutines.test.runTest
import kotlinx.serialization.json.Json
import kotlinx.serialization.json.JsonPrimitive
import kotlinx.serialization.json.buildJsonObject
import kotlinx.serialization.json.jsonObject
import kotlinx.serialization.json.jsonPrimitive
import kotlinx.serialization.json.put
import okhttp3.mockwebserver.MockResponse
import okhttp3.mockwebserver.MockWebServer
import kotlin.test.Test
import kotlin.test.assertEquals
import kotlin.test.assertIs
import kotlin.test.assertTrue

class VoiceControlApi065Test {
    @Test
    fun startUsesBoundNoStoreRequestAndStrictlyParsesSessionAndGrant() =
        runTest {
            MockWebServer().use { server ->
                server.enqueue(jsonResponse(200, startResponse()))
                val api = OkHttpVoiceControlApi(server.url("/").toString())

                val outcome = api.start(binding(), ACTIVATION_ID, capability())

                val started = assertIs<VoiceStartOutcome.Started>(outcome)
                assertEquals(SESSION_ID, started.session.sessionId)
                assertEquals(WORKER, started.grant.workerIdentity)
                assertTrue(started.grant.toString().contains("joinToken=[REDACTED]"))
                assertTrue(!started.grant.toString().contains(JOIN_TOKEN))

                val request = server.takeRequest()
                assertEquals("POST", request.method)
                assertEquals("/api/voice/sessions", request.path)
                assertEquals("Bearer user-token", request.getHeader("Authorization"))
                assertEquals(DEVICE_ID, request.getHeader("X-Astral-Device-Id"))
                assertEquals(CONNECTION_ID, request.getHeader("X-Astral-Connection-Generation"))
                assertEquals(CONTROL_VALUE, request.getHeader("X-Astral-Voice-Control-Binding"))
                assertEquals("no-store", request.getHeader("Cache-Control"))
                val body = Json.parseToJsonElement(request.body.readUtf8()).jsonObject
                assertEquals(ACTIVATION_ID, body["activation_id"]?.jsonPrimitive?.content)
                assertEquals("android", body["device_kind"]?.jsonPrimitive?.content)
                assertEquals(
                    "livekit",
                    body["capability"]?.jsonObject?.get("transport")?.jsonPrimitive?.content,
                )
            }
        }

    @Test
    fun takeoverConflictAndServerFailureRemainTypedAndContentSafe() =
        runTest {
            MockWebServer().use { server ->
                server.enqueue(
                    jsonResponse(
                        409,
                        """
                        {
                          "code":"voice_takeover_required",
                          "message":"Voice is active elsewhere.",
                          "owner":{
                            "session_id":"$SESSION_ID",
                            "device_kind":"ios",
                            "device_label":"Sam's iPhone",
                            "generation":3,
                            "media_grant_revision":4
                          }
                        }
                        """.trimIndent(),
                    ),
                )
                server.enqueue(jsonResponse(503, "{\"code\":\"voice_unavailable\",\"message\":\"Try later.\"}"))
                val api = OkHttpVoiceControlApi(server.url("/").toString())
                val target = VoiceTakeoverTarget(SESSION_ID, "ios", "Sam's iPhone", 3, 4)

                val takeover = api.takeover(binding(), ACTIVATION_ID, target, capability())
                val required = assertIs<VoiceStartOutcome.TakeoverRequired>(takeover)
                assertEquals(3, required.target.generation)
                assertEquals("Voice is active elsewhere.", required.message)
                val takeoverRequest = server.takeRequest()
                assertEquals("/api/voice/sessions/$SESSION_ID/takeover", takeoverRequest.path)
                val takeoverBody = Json.parseToJsonElement(takeoverRequest.body.readUtf8()).jsonObject
                assertEquals("3", takeoverBody["expected_generation"]?.jsonPrimitive?.content)
                assertEquals("4", takeoverBody["expected_media_grant_revision"]?.jsonPrimitive?.content)

                val failed = assertIs<VoiceStartOutcome.Failed>(api.start(binding(), ACTIVATION_ID, capability()))
                assertEquals("voice_unavailable", failed.reason)
                assertEquals("Try later.", failed.message)
            }
        }

    @Test
    fun updateStopAndEndCarryGenerationFencesAndFailClosedOnMalformedBodies() =
        runTest {
            MockWebServer().use { server ->
                server.enqueue(jsonResponse(200, sessionJson(microphoneEnabled = false)))
                server.enqueue(MockResponse().setResponseCode(202))
                server.enqueue(MockResponse().setResponseCode(204))
                server.enqueue(jsonResponse(200, "{\"session\":{},\"grant\":{}}"))
                val api = OkHttpVoiceControlApi(server.url("/").toString())
                val session = session()

                val updated =
                    api.update(
                        binding(),
                        session,
                        buildJsonObject { put("microphone_enabled", JsonPrimitive(false)) },
                    ).getOrThrow()
                assertEquals(false, updated.microphoneEnabled)
                val updateRequest = server.takeRequest()
                assertEquals("PATCH", updateRequest.method)
                assertEquals("Bearer user-token", updateRequest.getHeader("Authorization"))
                assertEquals(DEVICE_ID, updateRequest.getHeader("X-Astral-Device-Id"))
                assertEquals(CONNECTION_ID, updateRequest.getHeader("X-Astral-Connection-Generation"))
                assertEquals(CONTROL_VALUE, updateRequest.getHeader("X-Astral-Voice-Control-Binding"))
                val updateBody = Json.parseToJsonElement(updateRequest.body.readUtf8()).jsonObject
                assertEquals("1", updateBody["expected_generation"]?.jsonPrimitive?.content)
                assertEquals("1", updateBody["expected_media_grant_revision"]?.jsonPrimitive?.content)
                assertEquals("false", updateBody["microphone_enabled"]?.jsonPrimitive?.content)

                assertTrue(api.stopSpeech(binding(), session))
                assertEquals("/api/voice/sessions/$SESSION_ID/speech/stop", server.takeRequest().path)
                assertTrue(api.end(binding(), session))
                assertEquals(
                    "/api/voice/sessions/$SESSION_ID?expected_generation=1&expected_media_grant_revision=1",
                    server.takeRequest().path,
                )

                val malformed = assertIs<VoiceStartOutcome.Failed>(api.start(binding(), ACTIVATION_ID, capability()))
                assertEquals("voice_unavailable", malformed.reason)
            }
        }

    private fun binding() =
        VoiceUiBinding(
            token = "user-token",
            deviceId = DEVICE_ID,
            connectionGeneration = CONNECTION_ID,
            control =
                VoiceControlBinding(
                    DEVICE_ID,
                    CONNECTION_ID,
                    BINDING_ID,
                    CONTROL_VALUE,
                    "2026-07-31T12:10:00Z",
                ),
            visibleChatId = CHAT_ID,
        )

    private fun capability() = VoiceMediaCapability(true, true, "authorized", true)

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

    private fun startResponse(): String =
        """
        {
          "session":${sessionJson()},
          "grant":{
            "transport":"livekit",
            "grant_id":"grant-a",
            "session_id":"$SESSION_ID",
            "generation":1,
            "media_grant_revision":1,
            "expires_at":"2026-07-31T12:10:00Z",
            "url":"wss://voice.example.test",
            "join_token":"$JOIN_TOKEN",
            "room_name":"voice-room-a",
            "participant_identity":"voice-client-a",
            "worker_identity":"$WORKER"
          }
        }
        """.trimIndent()

    private fun sessionJson(microphoneEnabled: Boolean = true): String =
        """
        {
          "session_id":"$SESSION_ID",
          "device_id":"$DEVICE_ID",
          "owner_connection_generation":"$CONNECTION_ID",
          "visible_chat_id":"$CHAT_ID",
          "applied_visible_chat_id":"$CHAT_ID",
          "generation":1,
          "media_grant_revision":1,
          "chat_context_revision":1,
          "applied_chat_context_revision":1,
          "chat_context_synced":true,
          "state":"active",
          "foreground_active":true,
          "speech_muted":false,
          "microphone_enabled":$microphoneEnabled
        }
        """.trimIndent()

    private fun jsonResponse(
        status: Int,
        body: String,
    ): MockResponse =
        MockResponse()
            .setResponseCode(status)
            .setHeader("Content-Type", "application/json")
            .setBody(body)

    companion object {
        private const val DEVICE_ID = "00000000-0000-4000-8000-000000000001"
        private const val CONNECTION_ID = "00000000-0000-4000-8000-000000000002"
        private const val SESSION_ID = "00000000-0000-4000-8000-000000000003"
        private const val CHAT_ID = "00000000-0000-4000-8000-000000000004"
        private const val BINDING_ID = "00000000-0000-4000-8000-00000000000a"
        private const val ACTIVATION_ID = "00000000-0000-4000-8000-00000000000b"
        private const val CONTROL_VALUE = "synthetic-binding-value-000000000000"
        private const val JOIN_TOKEN = "synthetic-client-token-000000000000000000000000"
        private const val WORKER = "voice-worker-a"
    }
}
