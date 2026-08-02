package com.personalailabs.astraldeep.core.protocol

import kotlinx.serialization.json.Json
import kotlinx.serialization.json.JsonElement
import kotlinx.serialization.json.JsonObject
import kotlinx.serialization.json.JsonPrimitive
import kotlinx.serialization.json.contentOrNull
import kotlinx.serialization.json.intOrNull
import kotlinx.serialization.json.jsonArray
import kotlinx.serialization.json.jsonObject
import kotlinx.serialization.json.jsonPrimitive
import java.io.File
import kotlin.test.Test
import kotlin.test.assertEquals
import kotlin.test.assertIs
import kotlin.test.assertNotNull
import kotlin.test.assertNull
import kotlin.test.assertTrue

/** Feature 065 canonical Android wire conformance, loaded from the one shared C0-C6 fixture. */
class VoiceContract065Test {
    private val json = Json
    private val fixture by lazy {
        val relative = "backend/tests/fixtures/voice_065/client_conformance.json"
        val start = File(System.getProperty("user.dir")).absoluteFile
        val file =
            generateSequence(start) { it.parentFile }
                .map { File(it, relative) }
                .firstOrNull(File::isFile)
                ?: error("canonical voice fixture not found from $start")
        json.parseToJsonElement(file.readText()).jsonObject
    }

    private val rawVectors: List<JsonObject> by lazy {
        fixture["cases"]!!.jsonArray.flatMap { case ->
            val value = case.jsonObject
            value["positive"]!!.jsonArray.map(JsonElement::jsonObject) +
                value["negative"]!!.jsonArray.map(JsonElement::jsonObject)
        }
    }

    private val vectorsById: Map<String, JsonObject> by lazy {
        rawVectors.associateBy { it["id"]!!.jsonPrimitive.content }
    }

    @Test
    fun canonicalPositiveC0ThroughC6FramesDecodeToTypedAndroidMessages() {
        val supported =
            setOf(
                "composer_state",
                "voice_control_binding",
                "chat_created",
                "voice_session_state",
                "voice_turn_state",
                "voice_transcript",
                "user_message_acked",
                "voice_submission_rejected",
                "voice_announcement_media",
            )
        val decoded = mutableListOf<String>()
        rawVectors.filter { it["id"]!!.jsonPrimitive.content.contains("-P") }.forEach { vector ->
            val payload = materialize(vector)
            val type = payload["type"]?.jsonPrimitive?.contentOrNull ?: return@forEach
            if (type !in supported) return@forEach
            val message = Wire.decode(payload)
            assertTrue(message !is Inbound.Unknown, "${vector.id()} ($type) must decode")
            decoded += vector.id()
        }
        assertTrue(decoded.any { it.startsWith("C0-") })
        assertTrue(decoded.any { it.startsWith("C6-") })
    }

    @Test
    fun canonicalSchemaAndPacketNegativesFailClosed() {
        val ids =
            setOf(
                "C0-N1-extra-field",
                "C2-N1-final-missing-proof",
                "C2-N2-packet-too-large",
                "C3-N1-opening-too-long",
                "C3-N4-track-over-four-seconds",
                "C3-N5-non-result-continuation",
                "C3-N6-announcement-packet-too-large",
                "C4-N1-en-wrong-policy",
                "C5-N1-background-mic-enabled",
                "C6-N1-result-null-turn",
            )
        ids.forEach { id ->
            val payload = materialize(requireNotNull(vectorsById[id]))
            assertIs<Inbound.Unknown>(Wire.decode(payload.toString()), id)
        }
    }

    @Test
    fun voiceTurnSpeechOutcomeIsOptionalBoundedAndSuccessOnly() {
        val base = materialize(requireNotNull(vectorsById["C4-P1-en"])).toMutableMap()
        base["state"] = JsonPrimitive("succeeded")

        mapOf(
            "source_finished" to VoiceSpeechOutcome.SOURCE_FINISHED,
            "failed" to VoiceSpeechOutcome.FAILED,
            "suppressed" to VoiceSpeechOutcome.SUPPRESSED,
        ).forEach { (wireValue, expected) ->
            base["speech_outcome"] = JsonPrimitive(wireValue)
            val decoded = assertIs<Inbound.VoiceTurnStateFrame>(Wire.decode(JsonObject(base)))
            assertEquals(expected, decoded.value.speechOutcome)
        }

        base.remove("speech_outcome")
        val legacy = assertIs<Inbound.VoiceTurnStateFrame>(Wire.decode(JsonObject(base)))
        assertNull(legacy.value.speechOutcome)

        base["speech_outcome"] = JsonPrimitive("provider_detail")
        assertIs<Inbound.Unknown>(Wire.decode(JsonObject(base)))

        base["speech_outcome"] = JsonPrimitive("failed")
        base["state"] = JsonPrimitive("processing")
        assertIs<Inbound.Unknown>(Wire.decode(JsonObject(base)))
    }

    @Test
    fun strictCorrelatedNewChatEncoderMatchesCanonicalC1Vector() {
        val canonical = materialize(requireNotNull(vectorsById["C1-P2-new-chat"]))
        val encoded =
            Wire.encodeCorrelatedVoiceNewChat(
                connectionGeneration = canonical.string("connection_generation"),
                submissionId = canonical.string("submission_id"),
                requestGeneration = canonical.string("request_generation"),
            )
        assertEquals(canonical, json.parseToJsonElement(encoded))
    }

    @Test
    fun localPlayoutEncoderMatchesCanonicalContentFreeC3Vector() {
        val canonical = materialize(requireNotNull(vectorsById["C3-P3-playout"]))
        val encoded =
            Wire.encodeVoicePlayoutEvent(
                VoicePlayoutEvent(
                    deviceId = canonical.string("device_id"),
                    connectionGeneration = canonical.string("connection_generation"),
                    sessionId = canonical.string("session_id"),
                    generation = canonical.int("generation"),
                    mediaGrantRevision = canonical.int("media_grant_revision"),
                    announcementId = canonical.string("announcement_id"),
                    announcementSequence = canonical.int("announcement_sequence"),
                    turnId = canonical.string("turn_id"),
                    kind = canonical.string("kind"),
                    quantumRole = canonical.string("quantum_role"),
                    quantumIndex = canonical.int("quantum_index"),
                    resultReservedSamplesAfter = canonical.int("result_reserved_samples_after"),
                    phase = canonical.string("phase"),
                    clientSequence = canonical.int("client_sequence"),
                    observedAt = canonical.string("observed_at"),
                ),
            )
        assertEquals(canonical, json.parseToJsonElement(encoded))
        assertTrue(!encoded.contains("text"))
        assertTrue(!encoded.contains("track_sid"))
    }

    @Test
    fun localPlayoutEncoderRejectsResultAndIdentityContractDrift() {
        val base =
            VoicePlayoutEvent(
                deviceId = DEVICE_ID,
                connectionGeneration = CONNECTION_ID,
                sessionId = "00000000-0000-4000-8000-000000000003",
                generation = 1,
                mediaGrantRevision = 1,
                announcementId = "00000000-0000-4000-8000-000000000004",
                announcementSequence = 1,
                turnId = "00000000-0000-4000-8000-000000000005",
                kind = "result",
                quantumRole = "result_opening",
                quantumIndex = 0,
                resultReservedSamplesAfter = 36_000,
                phase = "started",
                clientSequence = 0,
                observedAt = "2026-07-31T12:00:00Z",
            )
        kotlin.test.assertFailsWith<IllegalArgumentException> {
            Wire.encodeVoicePlayoutEvent(base.copy(deviceId = "not-a-device"))
        }
        kotlin.test.assertFailsWith<IllegalArgumentException> {
            Wire.encodeVoicePlayoutEvent(base.copy(quantumRole = "single"))
        }
        kotlin.test.assertFailsWith<IllegalArgumentException> {
            Wire.encodeVoicePlayoutEvent(base.copy(resultReservedSamplesAfter = 36_001))
        }
    }

    @Test
    fun registrationAdvertisesRealAndroidVoiceFactsWithoutCredentials() {
        val frame =
            json.parseToJsonElement(
                Wire.encodeRegisterUi(
                    token = "user-bearer",
                    sessionId = null,
                    device =
                        DeviceCapabilities(
                            screenWidth = 1080,
                            screenHeight = 2400,
                            viewportWidth = 1080,
                            viewportHeight = 2400,
                            pixelRatio = 3.0,
                            deviceId = DEVICE_ID,
                            hasMicrophone = true,
                            hasAudioOutput = true,
                            microphonePermission = "authorized",
                            fullDuplex = true,
                        ),
                    connectionGeneration = CONNECTION_ID,
                ),
            ).jsonObject
        val device = frame["device"]!!.jsonObject
        assertTrue(frame["capabilities"]!!.jsonArray.any { it.jsonPrimitive.content == "voice" })
        assertEquals(DEVICE_ID, frame.string("device_id"))
        assertEquals(true, device["has_microphone"]!!.jsonPrimitive.content.toBoolean())
        assertEquals(true, device["has_audio_output"]!!.jsonPrimitive.content.toBoolean())
        assertEquals("authorized", device.string("microphone_permission"))
        assertEquals("livekit", device.string("voice_transport"))
        assertTrue(!frame.toString().contains("OPENAI", ignoreCase = true))
    }

    @Test
    fun transcriptOriginsAreFinalOnlyAndSensitiveValuesStayRedacted() {
        val binding =
            assertIs<Inbound.VoiceControlBindingFrame>(
                Wire.decode(materialize(requireNotNull(vectorsById["C1-P1-control-binding"]))),
            ).value
        assertTrue(binding.toString().contains("[REDACTED]"))
        assertTrue(!binding.toString().contains(binding.binding))

        val partial =
            assertIs<Inbound.VoiceTranscriptFrame>(
                Wire.decode(materialize(requireNotNull(vectorsById["C2-P1-partial"]))),
            ).value
        assertNull(partial.originOrNull())
        assertTrue(!partial.toString().contains(partial.text))

        val final =
            assertIs<Inbound.VoiceTranscriptFrame>(
                Wire.decode(materialize(requireNotNull(vectorsById["C2-P2-final"]))),
            ).value
        val origin = assertNotNull(final.originOrNull())
        assertEquals(final.sessionId, origin.sessionId)
        assertEquals(final.detectedLanguage, origin.detectedLanguage)
        assertTrue(origin.toString().contains("[REDACTED]"))
        assertTrue(!origin.toString().contains(requireNotNull(final.transcriptProof)))
        assertTrue(!final.toString().contains(final.text))
    }

    private fun materialize(vector: JsonObject): JsonObject {
        vector["payload"]?.let { return it.jsonObject }
        val base = vector["base_vector"]?.jsonPrimitive?.content ?: error("vector ${vector.id()} has no payload/base")
        var result = materialize(requireNotNull(vectorsById[base]))
        vector["mutations"]?.jsonArray?.forEach { mutation ->
            result = applyMutation(result, mutation.jsonObject)
        }
        return result
    }

    private fun applyMutation(
        root: JsonObject,
        mutation: JsonObject,
    ): JsonObject {
        val operation = mutation.string("op")
        val path = mutation.string("path").removePrefix("/").split('/').filter(String::isNotEmpty)
        require(path.isNotEmpty())
        val value =
            if (operation == "repeat") {
                val unit = mutation["value"]!!.jsonPrimitive.content
                val count = mutation["count"]!!.jsonPrimitive.intOrNull ?: error("repeat count")
                JsonPrimitive(unit.repeat(count))
            } else {
                mutation["value"]
            }
        return mutateObject(root, path, operation, value)
    }

    private fun mutateObject(
        root: JsonObject,
        path: List<String>,
        operation: String,
        value: JsonElement?,
    ): JsonObject {
        val key = path.first()
        if (path.size == 1) {
            return when (operation) {
                "remove" -> JsonObject(root - key)
                "add", "replace", "repeat" -> JsonObject(root + (key to requireNotNull(value)))
                else -> error("unsupported mutation $operation")
            }
        }
        val child = root[key]?.jsonObject ?: error("missing mutation parent $key")
        return JsonObject(root + (key to mutateObject(child, path.drop(1), operation, value)))
    }

    private fun JsonObject.id(): String = string("id")

    private fun JsonObject.string(key: String): String = this[key]!!.jsonPrimitive.content

    private fun JsonObject.int(key: String): Int = this[key]!!.jsonPrimitive.intOrNull ?: error("$key must be an int")

    companion object {
        private const val DEVICE_ID = "00000000-0000-4000-8000-000000000001"
        private const val CONNECTION_ID = "00000000-0000-4000-8000-000000000002"
    }
}
