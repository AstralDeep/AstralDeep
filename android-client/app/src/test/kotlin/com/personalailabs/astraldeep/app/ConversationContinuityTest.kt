package com.personalailabs.astraldeep.app

import com.personalailabs.astraldeep.app.auth.ConversationResumeStore
import com.personalailabs.astraldeep.app.auth.ConversationResumeStore.AccountIdentity
import com.personalailabs.astraldeep.app.auth.ConversationResumeStore.ClearReason
import com.personalailabs.astraldeep.app.rest.AstralRest
import com.personalailabs.astraldeep.app.transport.ConversationGenerationBinding
import com.personalailabs.astraldeep.app.transport.ConversationRequestPurpose
import com.personalailabs.astraldeep.app.transport.LocalSubmission
import com.personalailabs.astraldeep.app.transport.OrchestratorClient
import com.personalailabs.astraldeep.app.ui.AppViewModel
import com.personalailabs.astraldeep.app.ui.ChatSegmentKind
import com.personalailabs.astraldeep.app.ui.ChatTurn
import com.personalailabs.astraldeep.app.ui.UiState
import com.personalailabs.astraldeep.core.protocol.DeviceCapabilities
import com.personalailabs.astraldeep.core.protocol.Inbound
import com.personalailabs.astraldeep.core.protocol.ProtocolManifest
import com.personalailabs.astraldeep.core.protocol.SnapshotCanvas
import com.personalailabs.astraldeep.core.protocol.TransientFrameScope
import com.personalailabs.astraldeep.core.protocol.Wire
import com.personalailabs.astraldeep.core.sdui.Component
import kotlinx.serialization.json.Json
import kotlinx.serialization.json.JsonArray
import kotlinx.serialization.json.JsonObject
import kotlinx.serialization.json.buildJsonObject
import kotlinx.serialization.json.jsonObject
import kotlinx.serialization.json.jsonPrimitive
import kotlinx.serialization.json.put
import java.time.Instant
import java.util.Base64
import java.util.UUID
import kotlin.test.Test
import kotlin.test.assertEquals
import kotlin.test.assertFalse
import kotlin.test.assertIs
import kotlin.test.assertNotEquals
import kotlin.test.assertNotNull
import kotlin.test.assertNull
import kotlin.test.assertTrue

/** Spec 060 T048/T054 — Android's durable locator and atomic continuity reducer. */
class ConversationContinuityTest {
    private val chatId = "11111111-1111-4111-8111-111111111111"
    private val otherChatId = "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa"
    private val connection = "22222222-2222-4222-8222-222222222222"
    private val otherConnection = "bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbbb"
    private val hydrationRequest = "33333333-3333-4333-8333-333333333333"
    private val commitRequest = "44444444-4444-4444-8444-444444444444"
    private val submissionId = "77777777-7777-4777-8777-777777777777"
    private val snapshotId = "55555555-5555-4555-8555-555555555555"
    private val secondSnapshotId = "66666666-6666-4666-8666-666666666666"
    private val account = AccountIdentity("https://id.example/realms/astral", "user-17")
    private val now = Instant.parse("2026-07-16T12:00:00Z")

    private val vm =
        AppViewModel(
            OrchestratorClient("ws://localhost:9/ws"),
            AstralRest("http://localhost:9"),
        )

    @Test
    fun locator_is_account_scoped_minimal_and_survives_process_recreation() {
        val storage = MemoryStorage()
        val first = ConversationResumeStore(storage) { now }

        assertTrue(first.save(account, chatId))
        val key = ConversationResumeStore.storageKey(account)
        assertEquals(
            "astraldeep.active_chat.v1." +
                "4f4002a32804fce0ed9a0c2261f76d9cfc659a9f7f060afac3c5470c6dc3b663",
            key,
        )
        val raw = assertNotNull(storage.values[key])
        val value = Json.parseToJsonElement(raw).jsonObject
        assertEquals(setOf("schema_version", "chat_id", "updated_at"), value.keys)
        assertEquals(chatId, value["chat_id"]?.jsonPrimitive?.content)
        assertFalse(raw.contains("user-17"))
        assertFalse(raw.contains("id.example"))

        // A new Store instance represents Android process recreation.
        val recreated = ConversationResumeStore(storage) { now.plusSeconds(1) }
        assertEquals(chatId, recreated.load(account)?.chatId)
        assertNull(recreated.load(AccountIdentity(account.issuer, "other-user")))
    }

    @Test
    fun unknown_or_malformed_locator_is_retained_but_never_interpreted() {
        val storage = MemoryStorage()
        val store = ConversationResumeStore(storage) { now }
        val key = ConversationResumeStore.storageKey(account)
        storage.values[key] =
            """{"schema_version":2,"chat_id":"$chatId","updated_at":"$now","future":true}"""

        assertNull(store.load(account))
        assertTrue(storage.values.containsKey(key), "unknown schema must be retained for migration")

        storage.values[key] = "{malformed"
        assertNull(store.load(account))
        assertTrue(storage.values.containsKey(key), "transient/corrupt input is not an authorized clear")
    }

    @Test
    fun issuer_and_subject_come_from_the_keycloak_token_without_persisting_the_token() {
        val payload =
            Base64.getUrlEncoder().withoutPadding().encodeToString(
                """{"iss":"${account.issuer}","sub":"${account.subject}","email":"private@example.com"}"""
                    .encodeToByteArray(),
            )
        val token = "header.$payload.signature"

        assertEquals(account, ConversationResumeStore.accountFromAccessToken(token))
        assertNull(ConversationResumeStore.accountFromAccessToken("not-a-jwt"))
        assertNull(ConversationResumeStore.accountFromAccessToken("header.e30.signature"))
    }

    @Test
    fun only_the_four_definitive_actions_clear_the_current_accounts_locator() {
        ClearReason.entries.forEach { reason ->
            val storage = MemoryStorage()
            val store = ConversationResumeStore(storage) { now }
            assertTrue(store.save(account, chatId))

            assertTrue(store.clear(account, reason))
            assertNull(store.load(account), reason.name)
        }
        assertEquals(
            setOf(
                ClearReason.EXPLICIT_NEW_CHAT,
                ClearReason.DEFINITIVE_SIGN_OUT,
                ClearReason.ACCOUNT_SWITCH_OR_REMOVAL,
                ClearReason.CONFIRMED_DELETION,
            ),
            ClearReason.entries.toSet(),
        )

        val storage = MemoryStorage()
        val store = ConversationResumeStore(storage) { now }
        assertTrue(store.save(account, chatId))
        // Socket loss, process death, timeouts, and provider failures do not call clear.
        assertEquals(chatId, ConversationResumeStore(storage) { now }.load(account)?.chatId)
    }

    @Test
    fun registration_attempt_binds_locator_and_fresh_uuid4_before_register_frame() {
        val uuids =
            ArrayDeque(
                listOf(
                    connection,
                    hydrationRequest,
                    otherConnection,
                    commitRequest,
                ),
            )
        val client = OrchestratorClient("ws://localhost:9/ws", uuidFactory = { uuids.removeFirst() })

        val first =
            client.createRegistrationAttempt(
                token = "token",
                device = DeviceCapabilities(1080, 2340),
                activeChatId = chatId,
            )
        assertEquals(connection, first.binding.connectionGeneration)
        assertEquals(hydrationRequest, first.binding.requestGeneration)
        assertEquals(ConversationRequestPurpose.HYDRATION, first.binding.purpose)
        val frame = Json.parseToJsonElement(first.frame).jsonObject
        assertEquals(connection, frame["connection_generation"]?.jsonPrimitive?.content)
        assertEquals(
            hydrationRequest,
            frame["resume"]?.jsonObject?.get("request_generation")?.jsonPrimitive?.content,
        )

        val retry = client.createRegistrationAttempt("token", DeviceCapabilities(1080, 2340), chatId)
        assertNotEquals(first.binding.connectionGeneration, retry.binding.connectionGeneration)
        assertNotEquals(first.binding.requestGeneration, retry.binding.requestGeneration)
    }

    @Test
    fun intentional_load_and_turn_each_send_fresh_preserved_uuid4_operation_identity() {
        val client = OrchestratorClient("ws://localhost:9/ws")
        client.sendEvent("load_chat", chatId, buildJsonObject { put("chat_id", chatId) })
        client.sendChat("hello", chatId)
        client.sendChat("different normalized work", chatId)

        val frames = client.pendingFrames().map { raw -> Json.parseToJsonElement(raw).jsonObject }
        val requestIds = frames.map { it.getValue("request_generation").jsonPrimitive.content }
        val submissionIds = frames.map { it.getValue("submission_id").jsonPrimitive.content }
        assertEquals(3, requestIds.distinct().size)
        assertEquals(3, submissionIds.distinct().size)
        (requestIds + submissionIds).forEach { value ->
            val parsed = UUID.fromString(value)
            assertEquals(4, parsed.version())
            assertEquals(parsed.toString(), value)
        }
        frames.forEachIndexed { index, frame ->
            val payload = frame.getValue("payload").jsonObject
            assertEquals(requestIds[index], payload.getValue("request_generation").jsonPrimitive.content)
            assertEquals(submissionIds[index], payload.getValue("submission_id").jsonPrimitive.content)
            assertNotEquals(requestIds[index], submissionIds[index])
        }
    }

    @Test
    fun local_submission_callback_runs_before_transport_queueing() {
        val client = OrchestratorClient("ws://localhost:9/ws")
        var projected: LocalSubmission? = null

        val returned =
            client.sendEvent("discover_agents", null) { submission ->
                assertTrue(client.pendingActions().isEmpty())
                projected = submission
            }

        assertEquals(returned, projected)
        assertEquals(listOf("discover_agents"), client.pendingActions())
    }

    @Test
    fun first_equal_hydration_replaces_atomically_replay_is_noop_and_conflict_is_rejected() {
        val oldCanvas = listOf(component("text", "old", "Old canvas"))
        val base =
            vm.bindConversationGeneration(
                UiState(
                    activeChatId = chatId,
                    turns = listOf(com.personalailabs.astraldeep.app.ui.ChatTurn("assistant", "Old transcript")),
                    canvas = oldCanvas,
                    lastCommittedRenderRevision = 7UL,
                ),
                hydrationBinding(),
            )
        val incoming = snapshot(revision = 7UL, snapshotPurpose = "hydration")

        val applied = vm.reduce(base, incoming)
        assertEquals("The result is 21.", applied.turns.single().text)
        assertEquals("new", applied.canvas.single().id)
        assertEquals(7UL, applied.lastCommittedRenderRevision)
        assertTrue(applied.hydrationApplied)
        assertEquals(snapshotId, applied.acceptedSnapshotId)

        assertEquals(applied, vm.reduce(applied, incoming), "same-id same-content replay is idempotent")
        val conflict = incoming.copy(snapshotId = secondSnapshotId)
        assertEquals(applied, vm.reduce(applied, conflict), "different-id equal revision conflicts")
        val mutatedReplay = incoming.copy(canvas = SnapshotCanvas("canvas", oldCanvas))
        assertEquals(applied, vm.reduce(applied, mutatedReplay), "same id with different content conflicts")
    }

    @Test
    fun equal_commit_lower_revision_and_wrong_generations_cannot_change_committed_state() {
        val old =
            UiState(
                activeChatId = chatId,
                turns = listOf(com.personalailabs.astraldeep.app.ui.ChatTurn("assistant", "Stable")),
                canvas = listOf(component("text", "stable", "Stable")),
                lastCommittedRenderRevision = 9UL,
            )
        val commitFence = vm.bindConversationGeneration(old, commitBinding())
        assertEquals(commitFence, vm.reduce(commitFence, snapshot(9UL, "commit", request = commitRequest)))
        assertEquals(commitFence, vm.reduce(commitFence, snapshot(8UL, "commit", request = commitRequest)))
        assertEquals(
            commitFence,
            vm.reduce(commitFence, snapshot(10UL, "commit", request = hydrationRequest)),
        )
        assertEquals(
            commitFence,
            vm.reduce(commitFence, snapshot(10UL, "commit", connectionGeneration = otherConnection, request = commitRequest)),
        )
        assertEquals(
            commitFence,
            vm.reduce(commitFence, snapshot(10UL, "commit", chat = otherChatId, request = commitRequest)),
        )
    }

    @Test
    fun greater_commit_snapshot_replaces_transcript_and_canvas_in_one_state_change() {
        val old =
            vm.bindConversationGeneration(
                UiState(
                    activeChatId = chatId,
                    turns = listOf(com.personalailabs.astraldeep.app.ui.ChatTurn("assistant", "Old")),
                    canvas = listOf(component("text", "old", "Old")),
                    transientCanvas = listOf(component("text", "preview", "Preview")),
                    lastCommittedRenderRevision = 3UL,
                ),
                commitBinding(),
            )

        assertEquals(
            old,
            vm.reduce(old, snapshot(4UL, "commit", request = commitRequest)),
            "a commit snapshot cannot self-open its fence",
        )
        val opened =
            vm.reduce(
                old,
                Inbound.ConversationCommitReady(1, chatId, connection, commitRequest, 4UL),
            )
        val applied = vm.reduce(opened, snapshot(4UL, "commit", request = commitRequest))
        assertEquals("The result is 21.", applied.turns.single().text)
        assertEquals("new", applied.canvas.single().id)
        assertNull(applied.transientCanvas)
        assertEquals(4UL, applied.lastCommittedRenderRevision)
        assertNull(applied.requestGeneration)
        assertNull(applied.requestPurpose)
    }

    @Test
    fun scoped_render_frames_only_change_the_disposable_overlay_and_are_strictly_sequenced() {
        val committed = listOf(component("text", "committed", "Committed"))
        val base =
            vm.bindConversationGeneration(
                UiState(activeChatId = chatId, canvas = committed, lastCommittedRenderRevision = 4UL),
                commitBinding(),
            )
        val first =
            Inbound.UiRender(
                target = "canvas",
                components = listOf(component("text", "preview-1", "Preview 1")),
                scope = transientScope(sequence = 1UL),
            )
        val overlaid = vm.reduce(base, first)
        assertEquals(committed, overlaid.canvas, "preview must not mutate committed canvas")
        assertEquals("preview-1", overlaid.transientCanvas?.single()?.id)
        assertEquals(overlaid.transientCanvas, overlaid.visibleCanvas)

        val duplicate = first.copy(components = listOf(component("text", "duplicate", "Duplicate")))
        assertEquals(overlaid, vm.reduce(overlaid, duplicate))
        val wrongBase = first.copy(scope = transientScope(sequence = 2UL, baseRevision = 3UL))
        assertEquals(overlaid, vm.reduce(overlaid, wrongBase))
        val wrongRequest = first.copy(scope = transientScope(sequence = 2UL, request = hydrationRequest))
        assertEquals(overlaid, vm.reduce(overlaid, wrongRequest))
        val wrongChat = first.copy(scope = transientScope(sequence = 2UL, chat = otherChatId))
        assertEquals(overlaid, vm.reduce(overlaid, wrongChat))

        assertEquals(
            committed,
            vm.reduce(base, Inbound.ComponentDeleted("committed")).canvas,
            "an unscoped compatibility ack cannot mutate a 060 committed canvas",
        )
        assertEquals(
            committed,
            vm.reduce(base, Inbound.StreamSubscribed("stream-1", "tool", "committed")).canvas,
        )

        val second =
            first.copy(
                components = listOf(component("text", "preview-2", "Preview 2")),
                scope = transientScope(sequence = 2UL),
            )
        assertEquals("preview-2", vm.reduce(overlaid, second).transientCanvas?.single()?.id)
    }

    @Test
    fun successful_terminal_before_commit_snapshot_keeps_transient_answer_visible() {
        val committedCanvas = listOf(component("text", "committed", "Committed"))
        var state =
            vm.bindConversationGeneration(
                UiState(
                    activeChatId = chatId,
                    turns = listOf(ChatTurn("assistant", "Earlier answer")),
                    pendingTurns = listOf(ChatTurn("user", "Current question")),
                    canvas = committedCanvas,
                    preTurnCanvas = committedCanvas,
                    lastCommittedRenderRevision = 4UL,
                    turnActive = true,
                    pendingReplace = true,
                    statusText = "Running",
                ),
                commitBinding(),
            )
        state =
            vm.reduce(
                state,
                Inbound.UiRender(
                    target = "chat",
                    components = listOf(component("text", "answer", "Transient answer")),
                    scope = transientScope(sequence = 1UL),
                ),
            )
        state =
            vm.reduce(
                state,
                Inbound.UiRender(
                    target = "canvas",
                    components = listOf(component("text", "preview", "Transient canvas")),
                    scope = transientScope(sequence = 2UL),
                ),
            )

        val completed =
            vm.reduce(
                state,
                queuedOperation(
                    action = "chat_message",
                    chat = chatId,
                    connectionGeneration = connection,
                    requestGeneration = commitRequest,
                    state = "completed",
                    sequence = 1UL,
                ),
            )

        assertFalse(completed.hasActiveWork)
        assertNull(completed.workingStatusText)
        assertEquals(listOf("Current question", "Transient answer"), completed.pendingTurns.map { it.text })
        assertEquals("preview", completed.transientCanvas?.single()?.id)
        assertEquals(2UL, completed.lastTransientFrameSequence)
        assertFalse(completed.pendingReplace)

        val ready =
            vm.reduce(
                completed,
                Inbound.ConversationCommitReady(1, chatId, connection, commitRequest, 5UL),
            )
        val committed = vm.reduce(ready, snapshot(5UL, "commit", request = commitRequest))
        assertEquals("The result is 21.", committed.turns.single().text)
        assertTrue(committed.pendingTurns.isEmpty())
        assertNull(committed.transientCanvas)
    }

    @Test
    fun semantic_parts_are_visible_ordered_and_recovery_never_becomes_blank_debug_syntax() {
        val transcript =
            listOf(
                message(
                    parts =
                        JsonArray(
                            listOf(
                                jsonObject("""{"type":"text","text":"α text"}"""),
                                jsonObject(
                                    """{"type":"structured","value":{"rolls":[6,6,4],"total":16},"plain_text":"rolls: 6, 6, 4; total: 16"}""",
                                ),
                                jsonObject(
                                    """{"type":"components","components":[{"type":"text","content":"component value"}]}""",
                                ),
                                jsonObject(
                                    """{"type":"recovery","code":"saved_content_unrenderable","message":"A saved response could not be displayed."}""",
                                ),
                            ),
                        ),
                    attachments = JsonArray(listOf(jsonObject("""{"filename":"evidence.csv"}"""))),
                ),
            )
        val base =
            vm.bindConversationGeneration(
                UiState(activeChatId = chatId),
                hydrationBinding(),
            )

        val applied = vm.reduce(base, snapshot(1UL, "hydration", transcript = transcript))
        val turn = applied.turns.single()
        assertEquals(
            listOf(
                ChatSegmentKind.TEXT,
                ChatSegmentKind.STRUCTURED,
                ChatSegmentKind.COMPONENTS,
                ChatSegmentKind.RECOVERY,
            ),
            turn.segments.map { it.kind },
        )
        assertTrue(turn.text.contains("α text"))
        assertTrue(turn.text.contains("rolls: 6, 6, 4; total: 16"))
        assertTrue(turn.text.contains("component value"))
        assertTrue(turn.text.contains("A saved response could not be displayed."))
        assertFalse(turn.text.contains("{"))
        assertEquals("m-1", turn.messageId)
        assertEquals("2026-07-16T11:59:59Z", turn.createdAt)
        assertEquals("evidence.csv", turn.attachments.single()["filename"]?.jsonPrimitive?.content)
        assertEquals("component value", turn.segments[2].components.single().attributes["content"]?.jsonPrimitive?.content)
    }

    @Test
    fun blank_semantic_parts_fall_back_to_visible_recovery() {
        val blank =
            message(
                parts =
                    JsonArray(
                        listOf(
                            jsonObject("""{"type":"text","text":""}"""),
                            jsonObject("""{"type":"components","components":[]}"""),
                        ),
                    ),
            )
        val base = vm.bindConversationGeneration(UiState(activeChatId = chatId), hydrationBinding())
        val applied = vm.reduce(base, snapshot(0UL, "hydration", transcript = listOf(blank)))

        assertTrue(applied.turns.single().text.isNotBlank())
        assertTrue(applied.turns.single().segments.all { it.kind == ChatSegmentKind.RECOVERY })
    }

    @Test
    fun commit_ready_decoder_is_exact_and_reducer_opens_only_a_fresh_active_commit_fence() {
        assertEquals(ProtocolManifest.HANDLED, ProtocolManifest.classification["conversation_commit_ready"])
        val canonical =
            """
            {
              "type":"conversation_commit_ready",
              "schema_version":1,
              "chat_id":"$chatId",
              "connection_generation":"$connection",
              "request_generation":"$commitRequest",
              "render_revision":11
            }
            """.trimIndent()
        val ready = assertIs<Inbound.ConversationCommitReady>(Wire.decode(canonical))
        val active =
            vm.bindConversationGeneration(
                UiState(activeChatId = chatId, lastCommittedRenderRevision = 10UL),
                hydrationBinding(),
            )

        val opened = vm.reduce(active, ready)
        assertEquals(commitRequest, opened.requestGeneration)
        assertEquals(ConversationRequestPurpose.COMMIT, opened.requestPurpose)
        assertEquals(11UL, opened.expectedCommitRenderRevision)
        val committed = vm.reduce(opened, snapshot(11UL, "commit", request = commitRequest))
        assertEquals(11UL, committed.lastCommittedRenderRevision)

        val malformed =
            listOf(
                canonical.replace("\"render_revision\":11", "\"render_revision\":10"),
                canonical.dropLast(1) + ",\"unknown\":true}",
                canonical.replace(commitRequest, "not-a-uuid"),
                canonical.replace("\"schema_version\":1", "\"schema_version\":2"),
            )
        assertEquals(active, vm.reduce(active, assertIs<Inbound.ConversationCommitReady>(Wire.decode(malformed[0]))))
        malformed.drop(1).forEach { assertIs<Inbound.Unknown>(Wire.decode(it)) }
        assertEquals(active, vm.reduce(active, ready.copy(chatId = otherChatId)))
        assertEquals(active, vm.reduce(active, ready.copy(connectionGeneration = otherConnection)))
    }

    @Test
    fun chat_terminal_uses_retained_operation_fence_after_commit_snapshot_closes_request() {
        val uuids = ArrayDeque(listOf(submissionId, commitRequest))
        val client =
            OrchestratorClient(
                "ws://localhost:9/ws",
                uuidFactory = { uuids.removeFirst() },
            )
        val model = AppViewModel(client, AstralRest("http://localhost:9"))
        val local = client.sendChat("finish after snapshot", chatId)
        var state =
            model.projectLocalSubmission(
                model.bindConversationGeneration(UiState(activeChatId = chatId), commitBinding()),
                local,
            )
        state =
            model.reduce(
                state,
                Inbound.ConversationCommitReady(
                    schemaVersion = 1,
                    chatId = chatId,
                    connectionGeneration = connection,
                    requestGeneration = commitRequest,
                    renderRevision = 1UL,
                ),
            )

        val committed = model.reduce(state, snapshot(1UL, "commit", request = commitRequest))
        assertNull(committed.requestGeneration)
        assertEquals(local, committed.pendingSubmissions.getValue(commitRequest))

        val terminalStatus =
            Inbound.OperationStatus(
                operationId = secondSnapshotId,
                action = "chat_message",
                surface = "chat",
                chatId = chatId,
                connectionGeneration = connection,
                requestGeneration = commitRequest,
                sequence = 1UL,
                state = "completed",
                phase = "completed",
                label = "Completed",
                terminal = true,
                retryable = false,
                error = null,
                retryAfterMs = null,
                updatedAt = "2026-07-16T12:00:01Z",
            )
        assertEquals(committed, model.reduce(committed, terminalStatus.copy(chatId = otherChatId)))
        assertEquals(committed, model.reduce(committed, terminalStatus.copy(connectionGeneration = otherConnection)))

        val terminal = model.reduce(committed, terminalStatus)
        assertTrue(terminal.pendingSubmissions.isEmpty())
        assertNull(terminal.statusText)
        assertNull(terminal.workingStatusText)
    }

    @Test
    fun queued_surface_reconnect_restores_submitting_before_send_and_correlates_terminal() {
        val uuids = ArrayDeque(listOf(submissionId, commitRequest))
        val client =
            OrchestratorClient(
                "ws://localhost:9/ws",
                uuidFactory = { uuids.removeFirst() },
            )
        val model = AppViewModel(client, AstralRest("http://localhost:9"))
        val local = client.sendEvent("curated_example", null)
        val projected = model.projectLocalSubmission(UiState(), local)
        val disconnected = model.reduceConnectionState(projected, com.personalailabs.astraldeep.app.transport.ConnectionState.Disconnected)
        var replayed =
            model.bindConversationGeneration(
                disconnected,
                ConversationGenerationBinding(otherConnection, null, null, null),
            )
        val sent = mutableListOf<String>()

        client.replayPendingForTest(
            connectionGeneration = otherConnection,
            onGeneration = { binding -> replayed = model.bindConversationGeneration(replayed, binding) },
            onQueuedSubmission = { submission -> replayed = model.projectLocalSubmission(replayed, submission) },
            send = { frame -> sent.add(frame) },
        )

        assertEquals(local, replayed.pendingSubmissions.getValue(commitRequest))
        assertEquals("Submitting…", replayed.statusText)
        assertEquals(client.pendingActions(), emptyList())
        val replayedFrame = Json.parseToJsonElement(sent.single()).jsonObject
        assertEquals(submissionId, replayedFrame.getValue("submission_id").jsonPrimitive.content)
        assertEquals(commitRequest, replayedFrame.getValue("request_generation").jsonPrimitive.content)

        val accepted =
            model.reduce(
                replayed,
                queuedOperation(
                    action = "curated_example",
                    chat = null,
                    connectionGeneration = otherConnection,
                    requestGeneration = commitRequest,
                    state = "accepted",
                    sequence = 0UL,
                ),
            )
        assertEquals("Accepted", accepted.statusText)
        val completed =
            model.reduce(
                accepted,
                queuedOperation(
                    action = "curated_example",
                    chat = null,
                    connectionGeneration = otherConnection,
                    requestGeneration = commitRequest,
                    state = "completed",
                    sequence = 1UL,
                ),
            )
        assertTrue(completed.pendingSubmissions.isEmpty())
        assertNull(completed.statusText)
        assertNull(completed.workingStatusText)
    }

    @Test
    fun queued_chat_reconnect_rebinds_before_send_then_accepts_snapshot_and_terminal() {
        val uuids = ArrayDeque(listOf(submissionId, commitRequest))
        val client =
            OrchestratorClient(
                "ws://localhost:9/ws",
                uuidFactory = { uuids.removeFirst() },
            )
        val model = AppViewModel(client, AstralRest("http://localhost:9"))
        val local = client.sendChat("queued turn", chatId)
        val projected = model.projectLocalSubmission(UiState(activeChatId = chatId), local)
        val disconnected = model.reduceConnectionState(projected, com.personalailabs.astraldeep.app.transport.ConnectionState.Disconnected)
        var replayed =
            model.bindConversationGeneration(
                disconnected,
                ConversationGenerationBinding(otherConnection, chatId, hydrationRequest, ConversationRequestPurpose.HYDRATION),
            )
        val sent = mutableListOf<String>()

        client.replayPendingForTest(
            connectionGeneration = otherConnection,
            onGeneration = { binding -> replayed = model.bindConversationGeneration(replayed, binding) },
            onQueuedSubmission = { submission -> replayed = model.projectLocalSubmission(replayed, submission) },
            send = { frame -> sent.add(frame) },
        )

        assertEquals(otherConnection, replayed.connectionGeneration)
        assertEquals(commitRequest, replayed.requestGeneration)
        assertEquals(ConversationRequestPurpose.COMMIT, replayed.requestPurpose)
        assertEquals(local, replayed.pendingSubmissions.getValue(commitRequest))
        val replayedFrame = Json.parseToJsonElement(sent.single()).jsonObject
        assertEquals(submissionId, replayedFrame.getValue("submission_id").jsonPrimitive.content)
        assertEquals(commitRequest, replayedFrame.getValue("request_generation").jsonPrimitive.content)

        val accepted =
            model.reduce(
                replayed,
                queuedOperation(
                    action = "chat_message",
                    chat = chatId,
                    connectionGeneration = otherConnection,
                    requestGeneration = commitRequest,
                    state = "accepted",
                    sequence = 0UL,
                ),
            )
        val ready =
            model.reduce(
                accepted,
                Inbound.ConversationCommitReady(
                    schemaVersion = 1,
                    chatId = chatId,
                    connectionGeneration = otherConnection,
                    requestGeneration = commitRequest,
                    renderRevision = 1UL,
                ),
            )
        val committed =
            model.reduce(
                ready,
                snapshot(
                    revision = 1UL,
                    snapshotPurpose = "commit",
                    request = commitRequest,
                    connectionGeneration = otherConnection,
                ),
            )
        assertEquals(1UL, committed.lastCommittedRenderRevision)
        assertNull(committed.requestGeneration)
        assertEquals(local, committed.pendingSubmissions.getValue(commitRequest))

        val terminal =
            model.reduce(
                committed,
                queuedOperation(
                    action = "chat_message",
                    chat = chatId,
                    connectionGeneration = otherConnection,
                    requestGeneration = commitRequest,
                    state = "completed",
                    sequence = 1UL,
                ),
            )
        assertTrue(terminal.pendingSubmissions.isEmpty())
        assertNull(terminal.statusText)
        assertNull(terminal.workingStatusText)
    }

    @Test
    fun snapshot_failure_retries_only_the_exact_active_generation_without_clearing_state() {
        val active =
            vm.bindConversationGeneration(
                UiState(
                    activeChatId = chatId,
                    turns = listOf(com.personalailabs.astraldeep.app.ui.ChatTurn("assistant", "Committed")),
                    canvas = listOf(component("text", "committed", "Committed")),
                ),
                hydrationBinding(),
            )
        val retryable =
            Inbound.ErrorFrame(
                code = "snapshot_retryable",
                message = "temporarily unavailable",
                chatId = chatId,
                connectionGeneration = connection,
                requestGeneration = hydrationRequest,
                retryable = true,
            )

        assertEquals(chatId, vm.snapshotRetryTarget(active, retryable))
        assertNull(vm.snapshotRetryTarget(active, retryable.copy(retryable = false)))
        assertNull(vm.snapshotRetryTarget(active, retryable.copy(chatId = otherChatId)))
        assertNull(vm.snapshotRetryTarget(active, retryable.copy(connectionGeneration = otherConnection)))
        assertNull(vm.snapshotRetryTarget(active, retryable.copy(requestGeneration = commitRequest)))
        assertNull(vm.snapshotRetryTarget(active, retryable.copy(code = "chat_not_found")))
        assertEquals("Committed", active.turns.single().text)
        assertEquals("committed", active.canvas.single().id)
    }

    private fun hydrationBinding() =
        ConversationGenerationBinding(
            connectionGeneration = connection,
            chatId = chatId,
            requestGeneration = hydrationRequest,
            purpose = ConversationRequestPurpose.HYDRATION,
        )

    private fun commitBinding() =
        ConversationGenerationBinding(
            connectionGeneration = connection,
            chatId = chatId,
            requestGeneration = commitRequest,
            purpose = ConversationRequestPurpose.COMMIT,
        )

    private fun transientScope(
        sequence: ULong,
        baseRevision: ULong = 4UL,
        chat: String = chatId,
        request: String = commitRequest,
    ) = TransientFrameScope(chat, connection, request, baseRevision, sequence)

    private fun snapshot(
        revision: ULong,
        snapshotPurpose: String,
        request: String = hydrationRequest,
        connectionGeneration: String = connection,
        chat: String = chatId,
        transcript: List<JsonObject> = listOf(message()),
    ) = Inbound.ConversationSnapshot(
        schemaVersion = 1,
        snapshotId = snapshotId,
        chatId = chat,
        connectionGeneration = connectionGeneration,
        requestGeneration = request,
        snapshotPurpose = snapshotPurpose,
        renderRevision = revision,
        committedAt = "2026-07-16T12:00:00Z",
        transcript = transcript,
        canvas = SnapshotCanvas("canvas", listOf(component("text", "new", "New canvas"))),
    )

    private fun queuedOperation(
        action: String,
        chat: String?,
        connectionGeneration: String,
        requestGeneration: String,
        state: String,
        sequence: ULong,
    ) = Inbound.OperationStatus(
        operationId = secondSnapshotId,
        action = action,
        surface = if (chat == null) "operation" else "chat",
        chatId = chat,
        connectionGeneration = connectionGeneration,
        requestGeneration = requestGeneration,
        sequence = sequence,
        state = state,
        phase = state,
        label = state.replaceFirstChar(Char::uppercase),
        terminal = state == "completed",
        retryable = false,
        error = null,
        retryAfterMs = null,
        updatedAt = "2026-07-16T12:00:01Z",
    )

    private fun message(
        parts: JsonArray = JsonArray(listOf(jsonObject("""{"type":"text","text":"The result is 21."}"""))),
        attachments: JsonArray = JsonArray(emptyList()),
    ): JsonObject =
        buildJsonObject {
            put("message_id", "m-1")
            put("role", "assistant")
            put("created_at", "2026-07-16T11:59:59Z")
            put("parts", parts)
            put("attachments", attachments)
        }

    private fun component(
        type: String,
        id: String,
        content: String,
    ): Component =
        Component.fromJson(
            buildJsonObject {
                put("type", type)
                put("component_id", id)
                put("content", content)
            },
        )

    private fun jsonObject(raw: String): JsonObject = Json.parseToJsonElement(raw).jsonObject

    private class MemoryStorage : ConversationResumeStore.Storage {
        val values = mutableMapOf<String, String>()

        override fun get(key: String): String? = values[key]

        override fun put(
            key: String,
            value: String,
        ): Boolean {
            values[key] = value
            return true
        }

        override fun remove(key: String): Boolean {
            values.remove(key)
            return true
        }
    }
}
