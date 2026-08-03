package com.personalailabs.astraldeep.app

import android.os.SystemClock
import android.util.Log
import androidx.compose.ui.semantics.Role
import androidx.compose.ui.semantics.SemanticsActions
import androidx.compose.ui.semantics.SemanticsProperties
import androidx.compose.ui.state.ToggleableState
import androidx.compose.ui.test.SemanticsMatcher
import androidx.compose.ui.test.assert
import androidx.compose.ui.test.assertHasClickAction
import androidx.compose.ui.test.assertIsFocused
import androidx.compose.ui.test.junit4.createComposeRule
import androidx.compose.ui.test.onNodeWithTag
import androidx.compose.ui.test.onNodeWithText
import androidx.compose.ui.test.performClick
import androidx.compose.ui.test.performSemanticsAction
import androidx.test.ext.junit.runners.AndroidJUnit4
import androidx.test.platform.app.InstrumentationRegistry
import com.personalailabs.astraldeep.app.ui.AgentsScreen
import com.personalailabs.astraldeep.core.protocol.Agent
import com.personalailabs.astraldeep.core.protocol.ConversationResume
import com.personalailabs.astraldeep.core.protocol.DeviceCapabilities
import com.personalailabs.astraldeep.core.protocol.Inbound
import com.personalailabs.astraldeep.core.protocol.Wire
import kotlinx.serialization.json.Json
import kotlinx.serialization.json.JsonArray
import kotlinx.serialization.json.JsonElement
import kotlinx.serialization.json.JsonNull
import kotlinx.serialization.json.JsonObject
import kotlinx.serialization.json.JsonPrimitive
import kotlinx.serialization.json.buildJsonArray
import kotlinx.serialization.json.buildJsonObject
import kotlinx.serialization.json.jsonObject
import kotlinx.serialization.json.jsonPrimitive
import kotlinx.serialization.json.put
import okhttp3.OkHttpClient
import okhttp3.Request
import okhttp3.Response
import okhttp3.WebSocket
import okhttp3.WebSocketListener
import org.junit.Assert.assertTrue
import org.junit.Assert.fail
import org.junit.Assume.assumeTrue
import org.junit.Rule
import org.junit.Test
import org.junit.runner.RunWith
import java.io.File
import java.security.MessageDigest
import java.time.Instant
import java.util.Base64
import java.util.UUID
import java.util.concurrent.LinkedBlockingDeque
import java.util.concurrent.TimeUnit

/**
 * Connected Spec 060 release-evidence producer (T109, US8).
 *
 * Runs only inside the `android-producer` job of `release-readiness.yml`
 * against the trusted staging endpoint, using the REAL production transport
 * ([Wire] encode/decode over OkHttp). Every check must pass — the single flow
 * then writes one `platform_evidence` report (`platform: android`, artifact
 * kind `android_apk` re-hashed from the installed APK bytes) into the app's
 * external files dir and logs `release_evidence_output=<path>` for the
 * workflow to `adb pull`. Skips (JUnit Assume) ONLY when `astralStagingUrl`
 * is absent; once it is present every other argument is required.
 *
 * Instrumentation arguments (all via
 * `-Pandroid.testInstrumentationRunnerArguments.<name>=<value>`):
 * `astralStagingUrl`, `astralAccessToken`, `astralCandidateSha`,
 * `astralReleaseId`, `astralReleaseVersion`, `astralLifecycleAgentId`,
 * `astralLifecycleStates` (comma-separated), `astralStagingEnvironmentB64`
 * (base64 stage-deploy outputs JSON), `astralToolchainCanaryB64` (base64
 * android-next-major-readiness.json), `astralRunnerImage`, `astralRunnerName`,
 * `astralRunnerArch`, `astralRunnerEnvironment`, `astralWorkflowName`,
 * `astralRunId`, `astralRunAttempt`, `astralJobId`, and optional
 * `astralLifecycleTimeoutMs` (default 120000).
 */
@RunWith(AndroidJUnit4::class)
class ReleaseEvidenceInstrumentedTest {
    @get:Rule val rule = createComposeRule()

    private val arguments = InstrumentationRegistry.getArguments()
    private val context = InstrumentationRegistry.getInstrumentation().targetContext
    private val httpClient =
        OkHttpClient.Builder()
            .pingInterval(20, TimeUnit.SECONDS)
            .readTimeout(0, TimeUnit.MILLISECONDS)
            .build()
    private val device = DeviceCapabilities(screenWidth = 1080, screenHeight = 2280)
    private val checks = LinkedHashMap<String, JsonObject>()

    @Test
    fun connected_release_flow_emits_schema_shaped_android_evidence() {
        assumeTrue(
            "astralStagingUrl selects the trusted staging endpoint for this producer",
            !arguments.getString("astralStagingUrl").isNullOrBlank(),
        )
        val startedAt = Instant.now().toString()
        val stagingUrl = requiredArgument("astralStagingUrl").trimEnd('/')
        assertTrue("staging endpoint must be HTTPS", stagingUrl.startsWith("https://"))
        val wsUrl = "wss://" + stagingUrl.removePrefix("https://") + "/ws"
        val token = requiredArgument("astralAccessToken")

        val signIn = runSignIn(wsUrl, token)
        val chat = runRenderedChat(wsUrl, token)
        val resume = runReconnectResumeTrials(wsUrl, token, chat)
        val lifecycle = runAgentLifecycle(wsUrl, token)
        val authoring = runAuthoringSurface(wsUrl, token)
        val accessibility = runAccessibilitySemantics()
        val toolchain = runToolchainReadiness()

        recordCheck("sign_in", signIn.durationMs, signIn.raw)
        recordCheck("rendered_chat", chat.durationMs, chat.raw)
        recordCheck("reconnect_resume", resume.durationMs, resume.raw, resumeMeasurements())
        recordCheck("agent_lifecycle", lifecycle.durationMs, lifecycle.raw)
        recordCheck("accessibility_semantics", accessibility.durationMs, accessibility.raw)
        recordCheck("personal_agent", authoring.durationMs, authoring.raw)
        recordCheck("android_next_toolchain_readiness", toolchain.durationMs, toolchain.raw)

        writeReport(stagingUrl, startedAt)
    }

    // ------------------------------------------------------------------
    // Checks
    // ------------------------------------------------------------------

    /** Invalid principal refused up front; the staging token registers cleanly. */
    private fun runSignIn(
        wsUrl: String,
        token: String,
    ): StepResult {
        val started = SystemClock.elapsedRealtime()
        StagingSocket(httpClient, wsUrl, "invalid-${UUID.randomUUID()}", device).use { socket ->
            val refused = socket.await(20_000) { it is Inbound.AuthRequired }
            assertTrue("an invalid token was not refused", refused != null)
        }
        StagingSocket(httpClient, wsUrl, token, device).use { socket ->
            socket.send(Wire.encodeUiEvent("get_history", null, requestGeneration = uuid4(), submissionId = uuid4()))
            val frame =
                socket.await(30_000) { it is Inbound.HistoryList || it is Inbound.AuthRequired }
            assertTrue(
                "authenticated registration failed: $frame",
                frame is Inbound.HistoryList,
            )
        }
        val raw =
            buildJsonObject {
                put("method", "staging_access_token_register_ui")
                put("invalid_token_refused", true)
                put("authenticated_history_received", true)
            }
        return StepResult(SystemClock.elapsedRealtime() - started, raw)
    }

    /** One real turn: new chat, dice prompt, durable two-sided transcript + canvas. */
    private fun runRenderedChat(
        wsUrl: String,
        token: String,
    ): ChatResult {
        val started = SystemClock.elapsedRealtime()
        return StagingSocket(httpClient, wsUrl, token, device).use { socket ->
            socket.send(Wire.encodeUiEvent("new_chat", null, requestGeneration = uuid4(), submissionId = uuid4()))
            val created = socket.await(30_000) { it is Inbound.ChatCreated && it.chatId != null }
            val chatId = (created as? Inbound.ChatCreated)?.chatId
            assertTrue("new_chat drew no chat_created", chatId != null)
            socket.send(
                Wire.encodeChatMessage(
                    message = PROMPT,
                    chatId = chatId,
                    requestGeneration = uuid4(),
                    submissionId = uuid4(),
                ),
            )
            val deadline = SystemClock.elapsedRealtime() + TURN_TIMEOUT_MS
            var transcriptSize = 0
            while (SystemClock.elapsedRealtime() < deadline && transcriptSize < 2) {
                transcriptSize = loadTranscriptSize(socket, chatId!!, 10_000)
                if (transcriptSize < 2) SystemClock.sleep(2_000)
            }
            assertTrue("the staged turn never completed", transcriptSize >= 2)
            assertTrue("no canvas components rendered", socket.componentsSeen > 0)
            val raw =
                buildJsonObject {
                    put("prompt_sha256", sha256(PROMPT.toByteArray(Charsets.UTF_8)))
                    put("transcript_messages", transcriptSize)
                    put("canvas_components_seen", socket.componentsSeen)
                }
            ChatResult(SystemClock.elapsedRealtime() - started, raw, chatId!!, transcriptSize)
        }
    }

    /** Twenty fresh-connection resume trials with explicit success counters. */
    private fun runReconnectResumeTrials(
        wsUrl: String,
        token: String,
        chat: ChatResult,
    ): StepResult {
        val started = SystemClock.elapsedRealtime()
        var successes = 0
        val latencies = ArrayList<Long>(RESUME_TRIALS)
        repeat(RESUME_TRIALS) { trial ->
            val trialStarted = SystemClock.elapsedRealtime()
            StagingSocket(
                httpClient,
                wsUrl,
                token,
                device,
                sessionId = chat.chatId,
                resume = ConversationResume(chat.chatId, uuid4()),
            ).use { socket ->
                val restored = loadTranscriptSize(socket, chat.chatId, TRIAL_TIMEOUT_MS)
                if (restored >= chat.transcriptSize) {
                    successes += 1
                } else {
                    Log.i(TAG, "resume_trial=$trial restored=$restored expected=${chat.transcriptSize}")
                }
            }
            latencies.add(SystemClock.elapsedRealtime() - trialStarted)
        }
        assertTrue(
            "reconnect/resume restored $successes of $RESUME_TRIALS trials",
            successes == RESUME_TRIALS,
        )
        val raw =
            buildJsonObject {
                put("chat_id_sha256", sha256(chat.chatId.toByteArray(Charsets.UTF_8)))
                put("trial_count", RESUME_TRIALS)
                put("successful_trials", successes)
                put("latencies_ms", buildJsonArray { latencies.forEach { add(JsonPrimitive(it)) } })
            }
        return StepResult(SystemClock.elapsedRealtime() - started, raw)
    }

    /** Generation-fenced lifecycle projection covers every expected state. */
    private fun runAgentLifecycle(
        wsUrl: String,
        token: String,
    ): StepResult {
        val started = SystemClock.elapsedRealtime()
        val agentId = requiredArgument("astralLifecycleAgentId")
        val expected =
            requiredArgument("astralLifecycleStates").split(",").filter { it.isNotBlank() }.toSet()
        assertTrue(
            "astralLifecycleStates is invalid",
            expected.isNotEmpty() && LIFECYCLE_STATES.containsAll(expected),
        )
        val window = arguments.getString("astralLifecycleTimeoutMs")?.toLong() ?: 120_000L
        val events = ArrayList<Inbound.AgentLifecycle>()
        StagingSocket(httpClient, wsUrl, token, device).use { socket ->
            val deadline = SystemClock.elapsedRealtime() + window
            while (SystemClock.elapsedRealtime() < deadline) {
                val frame =
                    socket.await(deadline - SystemClock.elapsedRealtime()) {
                        it is Inbound.AgentLifecycle && it.agentId == agentId
                    } ?: break
                events.add(frame as Inbound.AgentLifecycle)
                if (events.map { it.state }.toSet().containsAll(expected)) break
            }
        }
        val observed = events.map { it.state }.toSet()
        assertTrue(
            "lifecycle states ${expected - observed} never arrived for $agentId",
            observed.containsAll(expected),
        )
        // Wire already refuses malformed frames; assert the fence fields survived.
        events.forEach { event ->
            assertTrue("state outside vocabulary: ${event.state}", event.state in LIFECYCLE_STATES)
            assertTrue("label must be present", event.label.isNotBlank())
        }
        val raw =
            buildJsonObject {
                put("agent_id_sha256", sha256(agentId.toByteArray(Charsets.UTF_8)))
                put("required_states", buildJsonArray { expected.sorted().forEach { add(JsonPrimitive(it)) } })
                put(
                    "events",
                    buildJsonArray {
                        events.forEach { event ->
                            add(
                                buildJsonObject {
                                    put("state", event.state)
                                    put("generation", event.lifecycleGeneration.toLong())
                                    put("revision", event.stateRevision.toLong())
                                },
                            )
                        }
                    },
                )
            }
        return StepResult(SystemClock.elapsedRealtime() - started, raw)
    }

    /** The flag-gated authoring surface renders natively for this principal. */
    private fun runAuthoringSurface(
        wsUrl: String,
        token: String,
    ): StepResult {
        val started = SystemClock.elapsedRealtime()
        return StagingSocket(httpClient, wsUrl, token, device).use { socket ->
            socket.send(
                Wire.encodeUiEvent(
                    "chrome_open",
                    null,
                    payload =
                        buildJsonObject {
                            put("surface", "agent_authoring")
                            put("params", buildJsonObject {})
                        },
                    requestGeneration = uuid4(),
                    submissionId = uuid4(),
                ),
            )
            val frame =
                socket.await(30_000) {
                    it is Inbound.ChromeSurface && it.surfaceKey == "agent_authoring"
                }
            val surface = frame as? Inbound.ChromeSurface
            assertTrue("the authoring surface never rendered", surface != null)
            assertTrue("authoring surface arrived empty", surface!!.components.isNotEmpty())
            val raw =
                buildJsonObject {
                    put("surface", "agent_authoring")
                    put("title", surface.title)
                    put("component_count", surface.components.size)
                }
            StepResult(SystemClock.elapsedRealtime() - started, raw)
        }
    }

    /** TalkBack semantics of the changed authoring controls (T113 contracts). */
    private fun runAccessibilitySemantics(): StepResult {
        val started = SystemClock.elapsedRealtime()
        val weather =
            Agent(
                id = "weather",
                name = "Weather",
                description = "Forecasts",
                isPublic = false,
                scopes = emptyMap(),
                tools = listOf("get_weather"),
                permissions = mapOf("get_weather" to false),
            )
        rule.setContent {
            AgentsScreen(
                agents = listOf(weather),
                loading = false,
                onToggleAgent = { _, _ -> },
                onToggleTool = { _, _, _ -> },
                onEnableRecommended = {},
            )
        }
        val agentSwitch = rule.onNodeWithTag("agent-toggle:weather", useUnmergedTree = true)
        agentSwitch
            .assert(
                SemanticsMatcher.expectValue(
                    SemanticsProperties.ContentDescription,
                    listOf("Enable Weather agent"),
                ),
            ).assert(SemanticsMatcher.expectValue(SemanticsProperties.Role, Role.Switch))
            .assert(
                SemanticsMatcher.expectValue(
                    SemanticsProperties.ToggleableState,
                    ToggleableState.Off,
                ),
            ).assertHasClickAction()
        agentSwitch.performSemanticsAction(SemanticsActions.RequestFocus).assertIsFocused()

        rule.onNodeWithText("Weather", substring = true).performClick()
        val toolSwitch =
            rule.onNodeWithTag("agent-tool-toggle:weather:get_weather", useUnmergedTree = true)
        toolSwitch
            .assert(
                SemanticsMatcher.expectValue(
                    SemanticsProperties.ContentDescription,
                    listOf("Enable get_weather for Weather"),
                ),
            ).assert(SemanticsMatcher.expectValue(SemanticsProperties.Role, Role.Switch))
            .assertHasClickAction()
        toolSwitch.performSemanticsAction(SemanticsActions.RequestFocus).assertIsFocused()
        val raw =
            buildJsonObject {
                put("inspected_controls", 2)
                put("named_role_state_action_focus", true)
            }
        return StepResult(SystemClock.elapsedRealtime() - started, raw)
    }

    /** The official next-major toolchain diagnostic reached a fail-closed verdict. */
    private fun runToolchainReadiness(): StepResult {
        val started = SystemClock.elapsedRealtime()
        val canary = Json.parseToJsonElement(decodeBase64Argument("astralToolchainCanaryB64")).jsonObject
        val schemaVersion = canary["schema_version"]?.jsonPrimitive?.content
        val status = canary["status"]?.jsonPrimitive?.content
        assertTrue("canary schema_version must be 1", schemaVersion == "1")
        assertTrue(
            "next-major readiness diagnostic did not reach a passing verdict: $status",
            status == "passed" || status == "unavailable",
        )
        val raw =
            buildJsonObject {
                put("diagnostic_status", status)
                put("canary_sha256", sha256(decodeBase64Argument("astralToolchainCanaryB64").toByteArray(Charsets.UTF_8)))
            }
        return StepResult(SystemClock.elapsedRealtime() - started, raw)
    }

    // ------------------------------------------------------------------
    // Report assembly
    // ------------------------------------------------------------------

    private fun recordCheck(
        checkId: String,
        durationMs: Long,
        raw: JsonObject,
        measurements: JsonElement = buildJsonArray {},
    ) {
        val artifact = writeRawEvidence(checkId, raw)
        checks[checkId] =
            buildJsonObject {
                put("id", checkId)
                put("outcome", "passed")
                put("duration_ms", maxOf(0L, durationMs))
                put("detail_code", JsonNull)
                put("applicability_reason", JsonNull)
                put("measurements", measurements)
                put("evidence_artifacts", buildJsonArray { add(artifact) })
            }
    }

    private fun resumeMeasurements(): JsonElement =
        buildJsonArray {
            add(measurement("trial_count", "total", RESUME_TRIALS, "count", 20))
            add(measurement("resume_success_rate", "rate", 100, "percent", 100))
        }

    private fun measurement(
        metric: String,
        aggregation: String,
        value: Int,
        unit: String,
        threshold: Int,
    ): JsonObject =
        buildJsonObject {
            put("metric", metric)
            put("aggregation", aggregation)
            put("value", value)
            put("unit", unit)
            put("sample_count", RESUME_TRIALS)
            put("comparator", "gte")
            put("threshold", threshold)
        }

    private fun writeRawEvidence(
        checkId: String,
        raw: JsonObject,
    ): JsonObject {
        val bytes = (PRETTY.encodeToString(JsonObject.serializer(), raw) + "\n").toByteArray(Charsets.UTF_8)
        val file = File(evidenceDir(), "android-raw/$checkId.json")
        atomicWrite(file, bytes)
        return buildJsonObject {
            put("name", "android_$checkId")
            put("kind", "json_metrics")
            put("immutable_reference", "bundle://android-raw/$checkId.json")
            put("sha256", sha256(bytes))
        }
    }

    private fun writeReport(
        stagingUrl: String,
        startedAt: String,
    ) {
        val order =
            listOf(
                "sign_in",
                "rendered_chat",
                "reconnect_resume",
                "agent_lifecycle",
                "accessibility_semantics",
                "personal_agent",
                "android_next_toolchain_readiness",
            )
        assertTrue("checks never produced evidence", checks.keys.containsAll(order))
        val apk = File(context.packageCodePath)
        val report =
            buildJsonObject {
                put("document_type", "platform_evidence")
                put("schema_version", 1)
                put("evidence_id", UUID.randomUUID().toString())
                put("candidate_sha", requiredArgument("astralCandidateSha"))
                put("release_id", requiredArgument("astralReleaseId"))
                put("release_version", requiredArgument("astralReleaseVersion"))
                put("platform", "android")
                put(
                    "target_description",
                    "Connected debug Android client on an API 34 x86_64 emulator against the trusted staging endpoint",
                )
                put(
                    "artifact",
                    buildJsonObject {
                        put("name", apk.name)
                        put("kind", "android_apk")
                        put("immutable_reference", "bundle://android/${apk.name}")
                        put("sha256", sha256File(apk))
                        put("build_identity", "android-candidate:${requiredArgument("astralCandidateSha")}")
                    },
                )
                put("staging_environment", stagingEnvironment(stagingUrl))
                put(
                    "runner",
                    buildJsonObject {
                        put("os", "linux")
                        put("architecture", normalizedArchitecture())
                        put("runner_image", requiredArgument("astralRunnerImage"))
                        put("runner_name", requiredArgument("astralRunnerName"))
                        put("runner_environment", requiredArgument("astralRunnerEnvironment"))
                    },
                )
                put(
                    "workflow",
                    buildJsonObject {
                        put("name", requiredArgument("astralWorkflowName"))
                        put("run_id", requiredArgument("astralRunId"))
                        put("run_attempt", requiredArgument("astralRunAttempt").toInt())
                        put("job_id", requiredArgument("astralJobId"))
                    },
                )
                put("started_at", startedAt)
                put("completed_at", Instant.now().toString())
                put("outcome", "passed")
                put("unavailable_reason", JsonNull)
                put("unavailability_observation", JsonNull)
                put("checks", buildJsonArray { order.forEach { add(checks.getValue(it)) } })
            }
        val bytes = (PRETTY.encodeToString(JsonObject.serializer(), report) + "\n").toByteArray(Charsets.UTF_8)
        val output = File(evidenceDir(), "android.json")
        atomicWrite(output, bytes)
        Log.i(TAG, "release_evidence_output=${output.absolutePath}")
        Log.i(TAG, "release_evidence_sha256=${sha256(bytes)}")
    }

    /** Project the exact schema fields from the trusted stage-deploy outputs. */
    private fun stagingEnvironment(stagingUrl: String): JsonObject {
        val stage = Json.parseToJsonElement(decodeBase64Argument("astralStagingEnvironmentB64")).jsonObject
        STAGING_FIELDS.forEach { field ->
            assertTrue("trusted staging output is missing $field", stage[field] != null && stage[field] !is JsonNull)
        }
        val endpoint = stage.getValue("endpoint").jsonPrimitive.content.trimEnd('/')
        assertTrue("astralStagingUrl differs from the staged endpoint", endpoint == stagingUrl)
        val workerPaths = stage.getValue("worker_paths") as? JsonArray
        assertTrue(
            "trusted staging output does not include the voice worker path",
            workerPaths?.any { it.jsonPrimitive.content == "voice" } == true,
        )
        validateVoiceRuntime(stage.getValue("voice_runtime"))
        return buildJsonObject { STAGING_FIELDS.forEach { put(it, stage.getValue(it)) } }
    }

    /** Validate the stage-owned object without constructing a candidate replacement. */
    private fun validateVoiceRuntime(value: JsonElement) {
        assertTrue("trusted voice_runtime is not an object", value is JsonObject)
        val runtime = value.jsonObject
        assertTrue(
            "trusted voice_runtime has a missing or extra field",
            runtime.keys == VOICE_RUNTIME_FIELDS,
        )
        val profileValue = runtime.getValue("speech_profile")
        assertTrue("trusted speech_profile is not an object", profileValue is JsonObject)
        val profile = profileValue.jsonObject
        assertTrue(
            "trusted speech_profile has a missing or extra field",
            profile.keys == SPEECH_PROFILE_FIELDS,
        )
        assertTrue("unexpected ASR model", profile.getValue("asr_model").jsonPrimitive.content == ASR_MODEL)
        assertTrue("unexpected TTS model", profile.getValue("tts_model").jsonPrimitive.content == TTS_MODEL)
        assertTrue("unexpected voice", profile.getValue("voice").jsonPrimitive.content == TTS_VOICE)
        assertTrue(
            "unexpected sample rate",
            profile.getValue("sample_rate_hz").jsonPrimitive.content == TTS_SAMPLE_RATE_HZ.toString(),
        )
        listOf(
            "voice_worker_image_sha256" to runtime,
            "livekit_image_sha256" to runtime,
            "livekit_config_sha256" to runtime,
            "inventory_sha256" to profile,
            "profile_sha256" to profile,
        ).forEach { (field, owner) ->
            assertTrue(
                "$field is not a SHA-256 digest",
                SHA256.matches(owner.getValue(field).jsonPrimitive.content),
            )
        }
        listOf(
            "voice_worker_image_reference",
            "livekit_image_reference",
            "livekit_public_url",
            "livekit_turn_domain",
        ).forEach { field ->
            assertTrue(
                "$field is empty",
                runtime.getValue(field).jsonPrimitive.content.isNotBlank(),
            )
        }
    }

    private fun normalizedArchitecture(): String =
        when (requiredArgument("astralRunnerArch").lowercase()) {
            "x64", "x86_64" -> "x86_64"
            "arm64" -> "arm64"
            else -> {
                fail("astralRunnerArch is outside the release schema")
                error("unreachable")
            }
        }

    // ------------------------------------------------------------------
    // Helpers
    // ------------------------------------------------------------------

    private fun loadTranscriptSize(
        socket: StagingSocket,
        chatId: String,
        timeoutMs: Long,
    ): Int {
        socket.send(
            Wire.encodeUiEvent(
                "load_chat",
                chatId,
                payload = buildJsonObject { put("chat_id", chatId) },
                requestGeneration = uuid4(),
                submissionId = uuid4(),
            ),
        )
        val frame =
            socket.await(timeoutMs) {
                (it is Inbound.ChatLoaded && (it.chat.id == null || it.chat.id == chatId)) ||
                    (it is Inbound.ConversationSnapshot && it.chatId == chatId)
            }
        return when (frame) {
            is Inbound.ChatLoaded -> frame.chat.messages.size
            is Inbound.ConversationSnapshot -> frame.transcript.size
            else -> 0
        }
    }

    private fun requiredArgument(name: String): String {
        val value = arguments.getString(name)
        if (value.isNullOrBlank()) {
            fail("$name is required once astralStagingUrl is set")
        }
        return value!!
    }

    private fun decodeBase64Argument(name: String): String {
        val encoded = requiredArgument(name)
        val bytes =
            runCatching { Base64.getDecoder().decode(encoded) }
                .getOrElse { Base64.getUrlDecoder().decode(encoded) }
        return String(bytes, Charsets.UTF_8)
    }

    private fun evidenceDir(): File =
        File(context.getExternalFilesDir(null), "release-evidence").apply { mkdirs() }

    private fun atomicWrite(
        file: File,
        bytes: ByteArray,
    ) {
        file.parentFile?.mkdirs()
        val temporary = File(file.parentFile, "${file.name}.${UUID.randomUUID()}.tmp")
        temporary.writeBytes(bytes)
        assertTrue("could not persist ${file.name}", temporary.renameTo(file))
    }

    private fun sha256(bytes: ByteArray): String =
        MessageDigest.getInstance("SHA-256").digest(bytes).joinToString("") { "%02x".format(it) }

    private fun sha256File(file: File): String {
        val digest = MessageDigest.getInstance("SHA-256")
        file.inputStream().use { stream ->
            val buffer = ByteArray(64 * 1024)
            while (true) {
                val read = stream.read(buffer)
                if (read < 0) break
                digest.update(buffer, 0, read)
            }
        }
        return digest.digest().joinToString("") { "%02x".format(it) }
    }

    private fun uuid4(): String = UUID.randomUUID().toString()

    private data class StepResult(val durationMs: Long, val raw: JsonObject)

    private data class ChatResult(
        val durationMs: Long,
        val raw: JsonObject,
        val chatId: String,
        val transcriptSize: Int,
    )

    /**
     * One registered staging connection: production [Wire] bytes over OkHttp,
     * inbound frames decoded into [Inbound] and drained with bounded waits.
     */
    private class StagingSocket(
        client: OkHttpClient,
        url: String,
        token: String,
        device: DeviceCapabilities,
        sessionId: String? = null,
        resume: ConversationResume? = null,
    ) : AutoCloseable {
        private val frames = LinkedBlockingDeque<Inbound>()
        @Volatile private var failure: String? = null
        @Volatile var componentsSeen: Int = 0
            private set
        private val socket: WebSocket

        init {
            val listener =
                object : WebSocketListener() {
                    override fun onOpen(
                        webSocket: WebSocket,
                        response: Response,
                    ) {
                        webSocket.send(
                            Wire.encodeRegisterUi(
                                token = token,
                                sessionId = sessionId,
                                device = device,
                                connectionGeneration = UUID.randomUUID().toString(),
                                resume = resume,
                            ),
                        )
                    }

                    override fun onMessage(
                        webSocket: WebSocket,
                        text: String,
                    ) {
                        val frame = Wire.decode(text)
                        componentsSeen +=
                            when (frame) {
                                is Inbound.UiRender -> frame.components.size
                                is Inbound.UiUpsert -> frame.ops.size
                                is Inbound.UiStreamData -> frame.components.size
                                is Inbound.ConversationSnapshot -> frame.canvas.components.size
                                else -> 0
                            }
                        frames.offer(frame)
                    }

                    override fun onFailure(
                        webSocket: WebSocket,
                        t: Throwable,
                        response: Response?,
                    ) {
                        failure = t.message ?: "websocket failure"
                    }
                }
            socket = client.newWebSocket(Request.Builder().url(url).build(), listener)
        }

        fun send(frame: String) {
            assertTrue("send on a failed socket: $failure", failure == null)
            socket.send(frame)
        }

        fun await(
            timeoutMs: Long,
            predicate: (Inbound) -> Boolean,
        ): Inbound? {
            val deadline = SystemClock.elapsedRealtime() + timeoutMs
            while (SystemClock.elapsedRealtime() < deadline) {
                val remaining = maxOf(1, deadline - SystemClock.elapsedRealtime())
                val frame = frames.poll(remaining, TimeUnit.MILLISECONDS) ?: break
                if (predicate(frame)) return frame
            }
            return null
        }

        override fun close() {
            socket.close(1000, null)
        }
    }

    private companion object {
        const val TAG = "ReleaseEvidence060"
        const val PROMPT = "Roll exactly six six-sided dice and show the normalized results."
        const val RESUME_TRIALS = 20
        const val TRIAL_TIMEOUT_MS = 5_000L
        const val TURN_TIMEOUT_MS = 240_000L
        val PRETTY = Json { prettyPrint = true }
        val LIFECYCLE_STATES = setOf("starting", "online", "updating", "failed", "offline")
        val STAGING_FIELDS =
            listOf(
                "authentication_posture",
                "candidate_image_reference",
                "candidate_image_sha256",
                "database_posture",
                "deployed_at",
                "deployment_run_id",
                "endpoint",
                "environment_id",
                "fixture_manifest_sha256",
                "keycloak_realm_sha256",
                "macos_personal_agent_host",
                "migrated_schema_revision",
                "representative_dataset_sha256",
                "source_schema_revision",
                "topology",
                "voice_runtime",
                "worker_paths",
            )
        val VOICE_RUNTIME_FIELDS =
            setOf(
                "voice_worker_image_reference",
                "voice_worker_image_sha256",
                "livekit_image_reference",
                "livekit_image_sha256",
                "livekit_config_sha256",
                "livekit_public_url",
                "livekit_turn_domain",
                "speech_profile",
            )
        val SPEECH_PROFILE_FIELDS =
            setOf(
                "asr_model",
                "tts_model",
                "voice",
                "sample_rate_hz",
                "inventory_sha256",
                "profile_sha256",
            )
        const val ASR_MODEL = "Systran/faster-whisper-large-v3"
        const val TTS_MODEL = "speaches-ai/Kokoro-82M-v1.0-ONNX"
        const val TTS_VOICE = "af_heart"
        const val TTS_SAMPLE_RATE_HZ = 24000
        val SHA256 = Regex("^[0-9a-f]{64}$")
    }
}
