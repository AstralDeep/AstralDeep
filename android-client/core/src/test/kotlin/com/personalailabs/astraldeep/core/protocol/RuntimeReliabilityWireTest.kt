package com.personalailabs.astraldeep.core.protocol

import kotlinx.serialization.json.Json
import kotlinx.serialization.json.JsonArray
import kotlinx.serialization.json.JsonElement
import kotlinx.serialization.json.JsonNull
import kotlinx.serialization.json.JsonObject
import kotlinx.serialization.json.JsonPrimitive
import kotlinx.serialization.json.jsonObject
import kotlinx.serialization.json.jsonPrimitive
import kotlin.test.Test
import kotlin.test.assertEquals
import kotlin.test.assertFailsWith
import kotlin.test.assertIs
import kotlin.test.assertNotNull
import kotlin.test.assertNull
import kotlin.test.assertTrue

class RuntimeReliabilityWireTest {
    private val json = Json

    private val chatId = "11111111-1111-4111-8111-111111111111"
    private val connectionGeneration = "22222222-2222-4222-8222-222222222222"
    private val requestGeneration = "33333333-3333-4333-8333-333333333333"
    private val snapshotId = "44444444-4444-4444-8444-444444444444"
    private val operationId = "55555555-5555-4555-8555-555555555555"
    private val revisionId = "66666666-6666-4666-8666-666666666666"
    private val runtimeId = "77777777-7777-4777-8777-777777777777"
    private val hostId = "88888888-8888-4888-8888-888888888888"
    private val hostSessionId = "99999999-9999-4999-8999-999999999999"
    private val submissionId = "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa"

    private fun objectFrom(raw: String): JsonObject = json.parseToJsonElement(raw).jsonObject

    private fun JsonObject.with(
        key: String,
        value: JsonElement,
    ): JsonObject = JsonObject(this + (key to value))

    private fun JsonObject.without(key: String): JsonObject = JsonObject(this - key)

    private fun snapshot(): JsonObject =
        objectFrom(
            """
            {
              "type":"conversation_snapshot",
              "schema_version":1,
              "snapshot_id":"$snapshotId",
              "chat_id":"$chatId",
              "connection_generation":"$connectionGeneration",
              "request_generation":"$requestGeneration",
              "snapshot_purpose":"hydration",
              "render_revision":27,
              "committed_at":"2026-07-15T18:41:00Z",
              "transcript":[{
                "message_id":"1842",
                "role":"assistant",
                "created_at":"2026-07-15T18:40:59Z",
                "parts":[
                  {"type":"text","text":"The result is 21."},
                  {"type":"components","components":[{"type":"text","content":"Visible"}]},
                  {"type":"structured","value":{"total":21},"plain_text":"total: 21"},
                  {"type":"recovery","code":"saved_content_unrenderable","message":"Could not display."}
                ],
                "attachments":[]
              }],
              "canvas":{"target":"canvas","components":[{"type":"card","component_id":"c1"}]}
            }
            """.trimIndent(),
        )

    private fun operation(): JsonObject =
        objectFrom(
            """
            {
              "type":"operation_status",
              "operation_id":"$operationId",
              "action":"chrome_llm_save",
              "surface":"llm_settings",
              "chat_id":null,
              "connection_generation":"$connectionGeneration",
              "request_generation":"$requestGeneration",
              "sequence":2,
              "state":"validating",
              "phase":"validating_credentials",
              "label":"Checking credentials",
              "terminal":false,
              "retryable":false,
              "error":null,
              "retry_after_ms":null,
              "updated_at":"2026-07-15T18:41:00Z"
            }
            """.trimIndent(),
        )

    private fun refusal(): JsonObject =
        objectFrom(
            """
            {
              "type":"error",
              "submission_id":"$submissionId",
              "accepted":false,
              "code":"capacity_exceeded",
              "message":"Try again shortly.",
              "retryable":true,
              "retry_after_ms":1000
            }
            """.trimIndent(),
        )

    private fun lifecycle(): JsonObject =
        objectFrom(
            """
            {
              "type":"agent_lifecycle",
              "agent_id":"ua-dice-4f3c2a",
              "revision_id":null,
              "runtime_instance_id":null,
              "lifecycle_generation":14,
              "state_revision":3,
              "state":"offline",
              "reason_code":null,
              "label":"Offline",
              "updated_at":"2026-07-15T18:41:00Z"
            }
            """.trimIndent(),
        )

    @Test
    fun decodesEveryCanonicalSnapshotFieldAndPurpose() {
        val hydration = assertIs<Inbound.ConversationSnapshot>(Wire.decode(snapshot()))

        assertEquals(1, hydration.schemaVersion)
        assertEquals(snapshotId, hydration.snapshotId)
        assertEquals(chatId, hydration.chatId)
        assertEquals(connectionGeneration, hydration.connectionGeneration)
        assertEquals(requestGeneration, hydration.requestGeneration)
        assertEquals("hydration", hydration.snapshotPurpose)
        assertEquals(27UL, hydration.renderRevision)
        assertEquals("2026-07-15T18:41:00Z", hydration.committedAt)
        assertEquals(1, hydration.transcript.size)
        assertEquals(4, hydration.transcript.single()["parts"]?.let { it as JsonArray }?.size)
        assertEquals("canvas", hydration.canvas.target)
        assertEquals(listOf("c1"), hydration.canvas.components.map { it.id })

        val commit =
            assertIs<Inbound.ConversationSnapshot>(
                Wire.decode(snapshot().with("snapshot_purpose", JsonPrimitive("commit"))),
            )
        assertEquals("commit", commit.snapshotPurpose)

        val maxRevision =
            assertIs<Inbound.ConversationSnapshot>(
                Wire.decode(
                    snapshot().with(
                        "render_revision",
                        json.parseToJsonElement("18446744073709551615"),
                    ),
                ),
            )
        assertEquals(ULong.MAX_VALUE, maxRevision.renderRevision)
    }

    @Test
    fun snapshotDecoderFailsClosedForIncompleteOrMalformedCanonicalShapes() {
        val invalidTranscript =
            JsonArray(
                listOf(
                    objectFrom(
                        """{"message_id":"1","role":"assistant","created_at":"2026-07-15T18:41:00Z","parts":[]}""",
                    ),
                ),
            )
        val invalidCanvas = objectFrom("""{"target":"canvas","components":null}""")
        val malformed =
            listOf(
                snapshot().without("canvas"),
                snapshot().with("extra", JsonPrimitive(true)),
                snapshot().with("schema_version", JsonPrimitive("1")),
                snapshot().with("snapshot_purpose", JsonPrimitive("preview")),
                snapshot().with("request_generation", JsonPrimitive("not-a-uuid")),
                snapshot().with("render_revision", JsonPrimitive(-1)),
                snapshot().with("committed_at", JsonPrimitive("2026-07-15T18:41:00+00:00")),
                snapshot().with("transcript", invalidTranscript),
                snapshot().with("canvas", invalidCanvas),
            )

        malformed.forEach { assertIs<Inbound.Unknown>(Wire.decode(it)) }
    }

    @Test
    fun textPartVariantIsBoundedToTheCanonicalSet() {
        // 066 T023 contract extension: a text part may carry an OPTIONAL
        // variant from the closed set {"caption"}; anything else fails closed.
        fun withTextPart(part: String): JsonObject {
            val transcript =
                JsonArray(
                    listOf(
                        objectFrom(
                            """{"message_id":"1","role":"assistant","created_at":"2026-07-15T18:41:00Z","parts":[$part],"attachments":[]}""",
                        ),
                    ),
                )
            return snapshot().with("transcript", transcript)
        }

        assertIs<Inbound.ConversationSnapshot>(
            Wire.decode(withTextPart("""{"type":"text","text":"As of July","variant":"caption"}""")),
        )
        assertIs<Inbound.Unknown>(
            Wire.decode(withTextPart("""{"type":"text","text":"words","variant":"h1"}""")),
        )
        assertIs<Inbound.Unknown>(
            Wire.decode(withTextPart("""{"type":"text","text":"words","weight":"caption"}""")),
        )
    }

    @Test
    fun decodesOperationStatusWithExplicitNullsAndTerminalError() {
        val active = assertIs<Inbound.OperationStatus>(Wire.decode(operation()))

        assertEquals(operationId, active.operationId)
        assertEquals("chrome_llm_save", active.action)
        assertEquals("llm_settings", active.surface)
        assertNull(active.chatId)
        assertEquals(connectionGeneration, active.connectionGeneration)
        assertEquals(requestGeneration, active.requestGeneration)
        assertEquals(2UL, active.sequence)
        assertEquals("validating", active.state)
        assertEquals("validating_credentials", active.phase)
        assertEquals("Checking credentials", active.label)
        assertEquals(false, active.terminal)
        assertEquals(false, active.retryable)
        assertNull(active.error)
        assertNull(active.retryAfterMs)
        assertEquals("2026-07-15T18:41:00Z", active.updatedAt)

        val retryable =
            operation()
                .with("chat_id", JsonPrimitive(chatId))
                .with("state", JsonPrimitive("retryable"))
                .with("phase", JsonPrimitive("provider_probe"))
                .with("terminal", JsonPrimitive(true))
                .with("retryable", JsonPrimitive(true))
                .with(
                    "error",
                    objectFrom("""{"code":"provider_unavailable","message":"Try again."}"""),
                ).with("retry_after_ms", JsonPrimitive(250))
        val terminal = assertIs<Inbound.OperationStatus>(Wire.decode(retryable))
        assertEquals(chatId, terminal.chatId)
        assertEquals("provider_unavailable", terminal.error?.code)
        assertEquals("Try again.", terminal.error?.message)
        assertEquals(250UL, terminal.retryAfterMs)
    }

    @Test
    fun operationDecoderRequiresExplicitNullsAndConsistentFlags() {
        val invalidTerminalError = objectFrom("""{"code":"internal_detail","message":"No"}""")
        val malformed =
            listOf(
                operation().without("chat_id"),
                operation().without("error"),
                operation().without("retry_after_ms"),
                operation().with("chat_id", JsonPrimitive("bad")),
                operation().with("terminal", JsonPrimitive(true)),
                operation().with("error", objectFrom("""{"code":"conflict","message":"No"}""")),
                operation().with("retry_after_ms", JsonPrimitive(1)),
                operation().with("sequence", JsonPrimitive(-1)),
                operation().with("phase", JsonPrimitive("Not Snake Case")),
                operation()
                    .with("state", JsonPrimitive("failed"))
                    .with("terminal", JsonPrimitive(true))
                    .with("error", invalidTerminalError),
            )

        malformed.forEach { assertIs<Inbound.Unknown>(Wire.decode(it)) }
    }

    @Test
    fun admissionRefusalDecoderAcceptsOnlyTheExactCanonicalEnvelope() {
        val canonicalCodes =
            listOf(
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
        canonicalCodes.forEach { code ->
            val parsed =
                assertIs<Inbound.AdmissionRefusal>(
                    Wire.decode(
                        refusal()
                            .with("code", JsonPrimitive(code))
                            .with("retry_after_ms", JsonNull),
                    ),
                )
            assertEquals(submissionId, parsed.submissionId)
            assertEquals(code, parsed.code)
            assertNull(parsed.retryAfterMs)
        }

        val nonRetryable =
            assertIs<Inbound.AdmissionRefusal>(
                Wire.decode(
                    refusal()
                        .with("code", JsonPrimitive("registration_required"))
                        .with("retryable", JsonPrimitive(false))
                        .with("retry_after_ms", JsonNull),
                ),
            )
        assertEquals(false, nonRetryable.retryable)

        val malformed =
            listOf(
                refusal().with("unexpected", JsonPrimitive(true)),
                refusal().without("retry_after_ms"),
                refusal().with("submission_id", JsonNull),
                refusal().with("submission_id", JsonPrimitive(submissionId.uppercase())),
                refusal().with("code", JsonPrimitive("raw_internal_trace")),
                refusal().with("message", JsonPrimitive("  ")),
                refusal().with("accepted", JsonPrimitive(true)),
                refusal().with("retryable", JsonPrimitive("true")),
                refusal()
                    .with("retryable", JsonPrimitive(false))
                    .with("retry_after_ms", JsonPrimitive(1)),
                refusal().with("retry_after_ms", JsonPrimitive(-1)),
                refusal().with("retry_after_ms", JsonPrimitive(1.5)),
                refusal().with("retry_after_ms", JsonPrimitive("1000")),
            )
        malformed.forEach { assertIs<Inbound.ErrorFrame>(Wire.decode(it)) }
    }

    @Test
    fun decodesLifecycleExplicitNullsAndBothGenerationFields() {
        val offline = assertIs<Inbound.AgentLifecycle>(Wire.decode(lifecycle()))

        assertEquals("ua-dice-4f3c2a", offline.agentId)
        assertNull(offline.revisionId)
        assertNull(offline.runtimeInstanceId)
        assertEquals(14UL, offline.lifecycleGeneration)
        assertEquals(3UL, offline.stateRevision)
        assertEquals("offline", offline.state)
        assertNull(offline.reasonCode)
        assertEquals("Offline", offline.label)

        val online =
            lifecycle()
                .with("revision_id", JsonPrimitive(revisionId))
                .with("runtime_instance_id", JsonPrimitive(runtimeId))
                .with("state", JsonPrimitive("online"))
                .with("label", JsonPrimitive("Online"))
                .with("reason_code", JsonPrimitive("host_lost"))
                .with(
                    "lifecycle_generation",
                    json.parseToJsonElement("18446744073709551615"),
                )
        val current = assertIs<Inbound.AgentLifecycle>(Wire.decode(online))
        assertEquals(revisionId, current.revisionId)
        assertEquals(runtimeId, current.runtimeInstanceId)
        assertEquals(ULong.MAX_VALUE, current.lifecycleGeneration)
        assertEquals("host_lost", current.reasonCode)
    }

    @Test
    fun lifecycleDecoderFailsClosedOnMissingNullOrInvalidActiveFence() {
        val malformed =
            listOf(
                lifecycle().without("reason_code"),
                lifecycle().with("reason_code", JsonPrimitive(3)),
                lifecycle().with("reason_code", JsonPrimitive("runtime_recovered")),
                lifecycle().with("state", JsonPrimitive("running")),
                lifecycle().with("state", JsonPrimitive("online")),
                lifecycle().with("lifecycle_generation", JsonPrimitive(-1)),
                lifecycle().with("updated_at", JsonPrimitive("not-a-time")),
            )

        malformed.forEach { assertIs<Inbound.Unknown>(Wire.decode(it)) }
    }

    @Test
    fun decodesAllOrNoneTransientGenerationScopesAndKeepsLegacyFrames() {
        val legacy =
            assertIs<Inbound.UiRender>(
                Wire.decode("""{"type":"ui_render","target":"canvas","components":[]}"""),
            )
        assertNull(legacy.scope)

        val scopeFields =
            mapOf(
                "chat_id" to JsonPrimitive(chatId),
                "connection_generation" to JsonPrimitive(connectionGeneration),
                "request_generation" to JsonPrimitive(requestGeneration),
                "base_render_revision" to JsonPrimitive(27),
                "frame_sequence" to JsonPrimitive(4),
            )

        fun scoped(raw: String): JsonObject =
            JsonObject(objectFrom(raw) + scopeFields)

        val render = assertIs<Inbound.UiRender>(Wire.decode(scoped("""{"type":"ui_render","components":[]}""")))
        val upsert = assertIs<Inbound.UiUpsert>(Wire.decode(scoped("""{"type":"ui_upsert","ops":[]}""")))
        val stream =
            assertIs<Inbound.UiStreamData>(
                Wire.decode(scoped("""{"type":"ui_stream_data","components":[]}""")),
            )
        listOf(render.scope, upsert.scope, stream.scope).forEach { scope ->
            assertNotNull(scope)
            assertEquals(chatId, scope.chatId)
            assertEquals(connectionGeneration, scope.connectionGeneration)
            assertEquals(requestGeneration, scope.requestGeneration)
            assertEquals(27UL, scope.baseRenderRevision)
            assertEquals(4UL, scope.frameSequence)
        }

        val legacyChatId =
            assertIs<Inbound.UiUpsert>(
                Wire.decode("""{"type":"ui_upsert","chat_id":"legacy","ops":[]}"""),
            )
        assertEquals("legacy", legacyChatId.chatId)
        assertNull(legacyChatId.scope)

        assertIs<Inbound.Unknown>(
            Wire.decode(
                """{"type":"ui_render","connection_generation":"$connectionGeneration","components":[]}""",
            ),
        )
        assertIs<Inbound.Unknown>(
            Wire.decode(scoped("""{"type":"ui_render","components":[]}""").with("frame_sequence", JsonPrimitive("4"))),
        )
    }

    @Test
    fun validatesStructuredHostShapesWhileGenericAndroidDecodeIgnoresAck() {
        val registrationJson =
            """
            {
              "host_id":"$hostId",
              "supported_runtime_contract_versions":[1,2],
              "runtime_lock_sha256":"${"a".repeat(64)}",
              "platform":"macos",
              "client_version":"1.2.3-beta.1+7"
            }
            """.trimIndent()
        val registration = assertNotNull(Wire.decodeAgentHostRegistration(registrationJson))
        assertEquals(hostId, registration.hostId)
        assertEquals(listOf(1, 2), registration.supportedRuntimeContractVersions)
        assertEquals("macos", registration.platform)

        val acknowledgementJson =
            """
            {
              "type":"agent_host_registered",
              "host_id":"$hostId",
              "host_session_id":"$hostSessionId",
              "inventory_required":true,
              "accepted_at":"2026-07-15T18:41:00Z"
            }
            """.trimIndent()
        val acknowledgement = assertNotNull(Wire.decodeAgentHostRegistered(acknowledgementJson))
        assertEquals(hostSessionId, acknowledgement.hostSessionId)
        assertTrue(acknowledgement.inventoryRequired)
        assertIs<Inbound.Unknown>(Wire.decode(acknowledgementJson))

        assertNull(
            Wire.decodeAgentHostRegistration(
                registrationJson.replace("[1,2]", "[2,1]"),
            ),
        )
        assertNull(Wire.decodeAgentHostRegistered(acknowledgementJson.replace("true", "\"true\"")))
    }

    @Test
    fun candidateCapabilityMapIsExactAndNeverDefaultsMalformedToFalse() {
        val unsupported =
            assertNotNull(
                Wire.decodeCandidateCapabilityMap(
                    """{"capabilities":{"personal_agent_host":{"macos":{"supported":false,"runtime_contract_versions":[],"source_feature":null}}}}""",
                ),
            )
        assertEquals(false, unsupported.macosPersonalAgentHost.supported)
        assertNull(unsupported.macosPersonalAgentHost.sourceFeature)

        val supported =
            assertNotNull(
                Wire.decodeCandidateCapabilityMap(
                    """{"capabilities":{"personal_agent_host":{"macos":{"supported":true,"runtime_contract_versions":[2],"source_feature":"059"}}}}""",
                ),
            )
        assertEquals(listOf(2), supported.macosPersonalAgentHost.runtimeContractVersions)
        assertEquals("059", supported.macosPersonalAgentHost.sourceFeature)

        assertNull(
            Wire.decodeCandidateCapabilityMap(
                """{"capabilities":{"personal_agent_host":{"macos":{"supported":false,"runtime_contract_versions":[2],"source_feature":null}}}}""",
            ),
        )
        assertNull(Wire.decodeCandidateCapabilityMap("""{"capabilities":{}}"""))
    }

    @Test
    fun registerUiAddsGenerationResumeButNeverDesktopHostFields() {
        val legacy =
            json.parseToJsonElement(
                Wire.encodeRegisterUi(
                    token = "TOK",
                    sessionId = null,
                    device = DeviceCapabilities(1080, 2340),
                ),
            ).jsonObject
        assertTrue("connection_generation" !in legacy)
        assertTrue("resume" !in legacy)
        assertTrue("agent_host" !in legacy)
        assertTrue("host_session_id" !in legacy)

        val generated =
            json.parseToJsonElement(
                Wire.encodeRegisterUi(
                    token = "TOK",
                    sessionId = chatId,
                    device = DeviceCapabilities(1080, 2340),
                    connectionGeneration = connectionGeneration,
                    resume = ConversationResume(chatId, requestGeneration),
                ),
            ).jsonObject
        assertEquals(connectionGeneration, generated["connection_generation"]?.jsonPrimitive?.content)
        val resume = generated["resume"]?.jsonObject
        assertEquals("1", resume?.get("schema_version")?.jsonPrimitive?.content)
        assertEquals(chatId, resume?.get("active_chat_id")?.jsonPrimitive?.content)
        assertEquals(requestGeneration, resume?.get("request_generation")?.jsonPrimitive?.content)
        assertTrue("agent_host" !in generated)

        assertFailsWith<IllegalArgumentException> {
            Wire.encodeRegisterUi("TOK", null, DeviceCapabilities(1, 1), connectionGeneration = "bad")
        }
        assertFailsWith<IllegalArgumentException> {
            Wire.encodeRegisterUi(
                "TOK",
                null,
                DeviceCapabilities(1, 1),
                resume = ConversationResume(chatId, requestGeneration),
            )
        }
    }
}
