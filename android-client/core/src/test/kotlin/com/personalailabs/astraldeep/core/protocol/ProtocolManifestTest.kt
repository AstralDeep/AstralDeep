package com.personalailabs.astraldeep.core.protocol

import kotlinx.serialization.json.Json
import kotlinx.serialization.json.boolean
import kotlinx.serialization.json.jsonArray
import kotlinx.serialization.json.jsonObject
import kotlinx.serialization.json.jsonPrimitive
import java.io.File
import kotlin.test.Test
import kotlin.test.assertEquals
import kotlin.test.assertTrue

/**
 * Feature 044 — Android protocol-coverage drift guard. The committed manifest
 * (`backend/shared/ui_protocol.json`) is the single source of the server->client
 * frame vocabulary; the app's classification table must cover it exactly, so a
 * new server frame type fails the build until it is deliberately classified.
 */
class ProtocolManifestTest {
    private val admissionRefusalCodes =
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

    private fun manifestFile(): File {
        var dir: File? = File(".").absoluteFile
        while (dir != null) {
            val candidate = File(dir, "backend/shared/ui_protocol.json")
            if (candidate.isFile) return candidate
            dir = dir.parentFile
        }
        error("backend/shared/ui_protocol.json not found walking up from ${File(".").absolutePath}")
    }

    private fun manifestPushTypes(): Set<String> {
        val root = Json.parseToJsonElement(manifestFile().readText()).jsonObject
        return root.getValue("push_types").jsonArray
            .map { it.jsonObject.getValue("name").jsonPrimitive.content }
            .toSet()
    }

    private fun manifestRoot() = Json.parseToJsonElement(manifestFile().readText()).jsonObject

    @Test
    fun classification_covers_manifest_exactly() {
        val push = manifestPushTypes()
        val classified = ProtocolManifest.classification.keys
        val missing = (push - classified).sorted()
        val stale = (classified - push).sorted()
        assertTrue(missing.isEmpty(), "server frame types the app has not classified: $missing")
        assertTrue(stale.isEmpty(), "app classifies frame types the server no longer sends: $stale")
    }

    @Test
    fun classification_values_are_valid() {
        val allowed = setOf(ProtocolManifest.HANDLED, ProtocolManifest.IGNORED)
        assertTrue(ProtocolManifest.classification.values.all { it in allowed })
    }

    @Test
    fun core_loop_frames_are_handled() {
        listOf(
            "ui_render", "ui_upsert", "chat_status", "error", "auth_required",
            "chrome_menu", "chrome_surface", "user_message_acked", "chat_step",
            "tool_progress", "task_started", "task_completed", "notification",
            "user_preferences", "workspace_timeline_mode", "conversation_snapshot",
            "operation_status", "agent_lifecycle",
        ).forEach { frame ->
            assertTrue(ProtocolManifest.isHandled(frame), "$frame must be handled per the parity matrix")
        }
    }

    @Test
    fun conversational_voice_frames_are_required_and_handled() {
        listOf(
            "composer_state",
            "voice_control_binding",
            "voice_session_state",
            "voice_turn_state",
            "voice_submission_rejected",
            "voice_transcript",
            "voice_announcement_media",
        ).forEach { frame ->
            assertEquals(
                ProtocolManifest.HANDLED,
                ProtocolManifest.classification[frame],
                "$frame is required by feature 065 and may not drift to ignored",
            )
        }
    }

    @Test
    fun author_only_client_explicitly_ignores_host_control_frames() {
        listOf(
            "agent_host_inventory_reconciled",
            "agent_host_registered",
            "agent_host_registration_refused",
        ).forEach { frame ->
            assertEquals(
                ProtocolManifest.IGNORED,
                ProtocolManifest.classification[frame],
                "Android is author-only and must explicitly ignore $frame",
            )
        }
    }

    @Test
    fun manifest_declares_structured_host_registration() {
        val registrations =
            manifestRoot().getValue("additive_fields").jsonArray.filter { entry ->
                val value = entry.jsonObject
                value["field"]?.jsonPrimitive?.content == "agent_host" &&
                    value["carried_on"]?.jsonArray?.map { it.jsonPrimitive.content } == listOf("register_ui")
            }
        assertEquals(1, registrations.size)
        assertEquals(
            setOf(
                "host_id",
                "supported_runtime_contract_versions",
                "runtime_lock_sha256",
                "platform",
                "client_version",
            ),
            registrations.single().jsonObject.getValue("shape").jsonObject.keys,
        )
    }

    @Test
    fun manifest_declares_exact_admission_refusal_contract() {
        val contract =
            manifestRoot()
                .getValue("frame_contracts")
                .jsonObject
                .getValue("admission_refusal")
                .jsonObject

        assertEquals("error", contract.getValue("type").jsonPrimitive.content)
        assertEquals(
            listOf(
                "type",
                "submission_id",
                "accepted",
                "code",
                "message",
                "retryable",
                "retry_after_ms",
            ),
            contract.getValue("exact_fields").jsonArray.map { it.jsonPrimitive.content },
        )
        assertEquals(
            "canonical_lowercase_uuid4",
            contract.getValue("submission_id").jsonPrimitive.content,
        )
        assertEquals(false, contract.getValue("accepted").jsonPrimitive.boolean)
        assertEquals(false, contract.getValue("additional_fields").jsonPrimitive.boolean)
        assertEquals(
            admissionRefusalCodes,
            contract.getValue("codes").jsonArray.map { it.jsonPrimitive.content },
        )
    }
}
