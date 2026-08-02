package com.personalailabs.astraldeep.core.protocol

import kotlinx.serialization.json.Json
import kotlinx.serialization.json.JsonObject
import kotlinx.serialization.json.JsonPrimitive
import kotlinx.serialization.json.contentOrNull
import kotlinx.serialization.json.jsonArray
import kotlinx.serialization.json.jsonObject
import kotlinx.serialization.json.jsonPrimitive
import kotlin.test.Test
import kotlin.test.assertEquals
import kotlin.test.assertIs
import kotlin.test.assertTrue

class WireTest {
    @Test
    fun encodes_identified_ui_event_at_envelope_and_payload_levels() {
        val requestGeneration = "00000000-0000-4000-8000-000000000011"
        val submissionId = "00000000-0000-4000-8000-000000000012"
        val encoded =
            Json.parseToJsonElement(
                Wire.encodeUiEvent(
                    action = "chat_message",
                    sessionId = "chatA",
                    payload = JsonObject(mapOf("message" to JsonPrimitive("hello"))),
                    requestGeneration = requestGeneration,
                    submissionId = submissionId,
                ),
            ).jsonObject
        assertEquals(requestGeneration, encoded["request_generation"]?.jsonPrimitive?.content)
        assertEquals(submissionId, encoded["submission_id"]?.jsonPrimitive?.content)
        val payload = encoded["payload"]!!.jsonObject
        assertEquals(requestGeneration, payload["request_generation"]?.jsonPrimitive?.content)
        assertEquals(submissionId, payload["submission_id"]?.jsonPrimitive?.content)
        assertEquals("hello", payload["message"]?.jsonPrimitive?.content)
    }

    @Test
    fun decodes_ui_render() {
        val r =
            assertIs<Inbound.UiRender>(
                Wire.decode("""{"type":"ui_render","target":"canvas","components":[{"type":"text","component_id":"c1","content":"hi"}]}"""),
            )
        assertEquals("canvas", r.target)
        assertEquals(1, r.components.size)
        assertEquals("text", r.components[0].type)
        assertEquals("c1", r.components[0].id)
    }

    @Test
    fun decodes_ui_upsert_ops() {
        val r =
            assertIs<Inbound.UiUpsert>(
                Wire.decode(
                    """{"type":"ui_upsert","chat_id":"chatA","ops":[{"op":"upsert","component_id":"c1","component":{"type":"card"}},{"op":"remove","component_id":"c2"}]}""",
                ),
            )
        assertEquals("chatA", r.chatId)
        assertEquals(2, r.ops.size)
        assertEquals("upsert", r.ops[0].op)
        assertEquals("card", r.ops[0].component?.type)
        assertEquals("remove", r.ops[1].op)
    }

    @Test
    fun decodes_chrome_surface() {
        val r =
            assertIs<Inbound.ChromeSurface>(
                Wire.decode(
                    """{"type":"chrome_surface","region":"modal","surface_key":"theme","title":"Theme","components":[{"type":"button","label":"Apply","action":"chrome_theme_preset"},{"type":"color_picker","color_key":"bg","value":"#0F1221"}]}""",
                ),
            )
        assertEquals("theme", r.surfaceKey)
        assertEquals("Theme", r.title)
        assertEquals(2, r.components.size)
        assertEquals("button", r.components[0].type)
        assertEquals("color_picker", r.components[1].type)
        // No `mode` on the wire — defaults to today's behavior (054).
        assertEquals("replace", r.mode)
    }

    @Test
    fun decodes_chrome_surface_mandatory_mode() {
        val r =
            assertIs<Inbound.ChromeSurface>(
                Wire.decode(
                    """{"type":"chrome_surface","surface_key":"llm","title":"Set up your AI provider","components":[{"type":"text","content":"form"}],"mode":"mandatory"}""",
                ),
            )
        assertEquals("llm", r.surfaceKey)
        assertEquals("mandatory", r.mode)
    }

    @Test
    fun decodes_ui_stream_data() {
        val r =
            assertIs<Inbound.UiStreamData>(
                Wire.decode(
                    """{"type":"ui_stream_data","stream_id":"s1","session_id":"chatA","seq":3,"components":[{"type":"text"}],"terminal":false}""",
                ),
            )
        assertEquals("s1", r.streamId)
        assertEquals(3, r.seq)
        assertEquals(false, r.terminal)
        assertEquals(1, r.components.size)
        // No component_id on the wire (legacy/narrative stream) — null, never a default.
        assertEquals(null, r.componentId)
    }

    @Test
    fun decodes_stream_component_id_additive_field() {
        val data =
            assertIs<Inbound.UiStreamData>(
                Wire.decode(
                    """{"type":"ui_stream_data","stream_id":"s1","seq":1,"components":[{"type":"text"}],"component_id":"wc_abc"}""",
                ),
            )
        assertEquals("wc_abc", data.componentId)
        val sub =
            assertIs<Inbound.StreamSubscribed>(
                Wire.decode("""{"type":"stream_subscribed","stream_id":"s1","tool_name":"ticker","component_id":"wc_abc"}"""),
            )
        assertEquals("wc_abc", sub.componentId)
        val bare = assertIs<Inbound.StreamSubscribed>(Wire.decode("""{"type":"stream_subscribed","stream_id":"s1","tool_name":"ticker"}"""))
        assertEquals(null, bare.componentId)
    }

    @Test
    fun decodes_stream_error_push_shape() {
        val r =
            assertIs<Inbound.StreamErrorMsg>(
                Wire.decode(
                    """{"type":"stream_error","request_action":"stream_subscribe","session_id":"chatA","payload":{"stream_id":"s1","code":"blocked","message":"no"}}""",
                ),
            )
        assertEquals("s1", r.streamId)
        assertEquals("blocked", r.error.code)
        assertEquals("no", r.error.message)
    }

    @Test
    fun decodes_legacy_stream_error_and_unsubscribe_frames() {
        val error =
            assertIs<Inbound.StreamErrorMsg>(
                Wire.decode(
                    """{"type":"stream_error","session_id":"chatA","tool_name":"ticker","error":"offline"}""",
                ),
            )
        assertEquals("ticker", error.toolName)
        assertEquals("offline", error.error.code)
        assertEquals("offline", error.error.message)

        val unsubscribed =
            assertIs<Inbound.StreamUnsubscribed>(
                Wire.decode("""{"type":"stream_unsubscribed","tool_name":"ticker"}"""),
            )
        assertEquals("ticker", unsubscribed.toolName)
    }

    @Test
    fun decodes_agent_list_with_scopes() {
        val r =
            assertIs<Inbound.AgentList>(
                Wire.decode(
                    """{"type":"agent_list","agents":[{"id":"a1","name":"Weather","description":"d","is_public":true,"scopes":{"tools:read":true,"tools:write":false}}]}""",
                ),
            )
        assertEquals(1, r.agents.size)
        assertTrue(r.agents[0].isPublic)
        assertEquals(true, r.agents[0].scopes["tools:read"])
        assertEquals(false, r.agents[0].scopes["tools:write"])
    }

    @Test
    fun decodes_chat_loaded_transcript() {
        val r =
            assertIs<Inbound.ChatLoaded>(
                Wire.decode("""{"type":"chat_loaded","chat":{"id":"chatA","messages":[{"role":"user","content":"hey"}]}}"""),
            )
        assertEquals("chatA", r.chat.id)
        assertEquals("user", r.chat.messages[0].role)
        assertEquals("hey", r.chat.messages[0].content)
    }

    @Test
    fun decodes_chrome_render_and_auth_required() {
        assertIs<Inbound.ChromeRender>(Wire.decode("""{"type":"chrome_render","region":"modal","html":"<div/>"}"""))
        assertIs<Inbound.AuthRequired>(Wire.decode("""{"type":"auth_required","reason":"x"}"""))
    }

    @Test
    fun decodes_error_code_message_shape() {
        val r = assertIs<Inbound.ErrorFrame>(Wire.decode("""{"type":"error","code":"forbidden","message":"Nope"}"""))
        assertEquals("forbidden", r.code)
        assertEquals("Nope", r.message)
    }

    @Test
    fun decodes_error_payload_shape() {
        val r = assertIs<Inbound.ErrorFrame>(Wire.decode("""{"type":"error","payload":{"message":"Task t1 not found"}}"""))
        assertEquals(null, r.code)
        assertEquals("Task t1 not found", r.message)
    }

    @Test
    fun decodes_error_bare_message_shape() {
        val r = assertIs<Inbound.ErrorFrame>(Wire.decode("""{"type":"error","message":"boom"}"""))
        assertEquals("boom", r.message)
    }

    @Test
    fun decodes_error_with_no_message_to_default() {
        val r = assertIs<Inbound.ErrorFrame>(Wire.decode("""{"type":"error"}"""))
        assertEquals("Something went wrong.", r.message)
    }

    @Test
    fun decodes_chat_step() {
        val r =
            assertIs<Inbound.ChatStep>(
                Wire.decode(
                    """{"type":"chat_step","chat_id":"c1","step":{"id":"s1","name":"web_search","kind":"tool_call","status":"completed"}}""",
                ),
            )
        assertEquals("s1", r.id)
        assertEquals("web_search", r.name)
        assertEquals("completed", r.status)
    }

    @Test
    fun chat_step_tolerates_missing_fields() {
        val kindOnly = assertIs<Inbound.ChatStep>(Wire.decode("""{"type":"chat_step","step":{"kind":"tool_call"}}"""))
        assertEquals("tool_call", kindOnly.name)
        val bare = assertIs<Inbound.ChatStep>(Wire.decode("""{"type":"chat_step"}"""))
        assertEquals(null, bare.id)
        assertEquals(null, bare.name)
        assertEquals(null, bare.status)
    }

    @Test
    fun decodes_tool_progress_label() {
        val r =
            assertIs<Inbound.ToolProgress>(
                Wire.decode("""{"type":"tool_progress","tool_name":"web_search","agent_id":"a1","message":"fetching results","percentage":40}"""),
            )
        assertEquals("web_search: fetching results (40%)", r.label)
        val bare = assertIs<Inbound.ToolProgress>(Wire.decode("""{"type":"tool_progress"}"""))
        assertEquals("Working…", bare.label)
    }

    @Test
    fun decodes_task_started_and_completed_payload_shape() {
        val started = assertIs<Inbound.TaskStarted>(Wire.decode("""{"type":"task_started","payload":{"task_id":"t1","chat_id":"c1","status":"queued"}}"""))
        assertEquals("t1", started.taskId)
        assertEquals("c1", started.chatId)
        val done = assertIs<Inbound.TaskCompleted>(Wire.decode("""{"type":"task_completed","payload":{"task_id":"t1","chat_id":"c1","status":"completed"}}"""))
        assertEquals("t1", done.taskId)
        assertEquals("c1", done.chatId)
    }

    @Test
    fun decodes_task_frames_flat_shape() {
        val started = assertIs<Inbound.TaskStarted>(Wire.decode("""{"type":"task_started","task_id":"t2"}"""))
        assertEquals("t2", started.taskId)
        assertEquals(null, started.chatId)
        val done = assertIs<Inbound.TaskCompleted>(Wire.decode("""{"type":"task_completed","task_id":"t2","chat_id":"c2"}"""))
        assertEquals("t2", done.taskId)
        assertEquals("c2", done.chatId)
    }

    @Test
    fun decodes_notification() {
        val r =
            assertIs<Inbound.Notification>(
                Wire.decode("""{"type":"notification","level":"info","source":"schedule","job_id":"j1","chat_id":"c1","title":"Daily brief","body":"Ready"}"""),
            )
        assertEquals("Daily brief", r.title)
        assertEquals("Ready", r.body)
        assertEquals("info", r.level)
        assertEquals("c1", r.chatId)
    }

    @Test
    fun decodes_user_preferences_theme() {
        val r =
            assertIs<Inbound.UserPreferences>(
                Wire.decode("""{"type":"user_preferences","preferences":{"theme":{"preset":"ocean"},"other":1}}"""),
            )
        assertEquals("ocean", r.theme?.get("preset")?.jsonPrimitive?.contentOrNull)
    }

    @Test
    fun user_preferences_without_theme_is_null_theme() {
        val r = assertIs<Inbound.UserPreferences>(Wire.decode("""{"type":"user_preferences","preferences":{"locale":"en"}}"""))
        assertEquals(null, r.theme)
    }

    @Test
    fun decodes_workspace_timeline_mode() {
        assertEquals(true, assertIs<Inbound.WorkspaceTimelineMode>(Wire.decode("""{"type":"workspace_timeline_mode","active":true}""")).active)
        assertEquals(false, assertIs<Inbound.WorkspaceTimelineMode>(Wire.decode("""{"type":"workspace_timeline_mode","active":false}""")).active)
        // `on` is tolerated as an alias; a bare frame defaults to inactive.
        assertEquals(true, assertIs<Inbound.WorkspaceTimelineMode>(Wire.decode("""{"type":"workspace_timeline_mode","on":true}""")).active)
        assertEquals(false, assertIs<Inbound.WorkspaceTimelineMode>(Wire.decode("""{"type":"workspace_timeline_mode"}""")).active)
    }

    @Test
    fun decodes_workspace_verb_acks() {
        val saved = assertIs<Inbound.ComponentSaved>(Wire.decode("""{"type":"component_saved","component":{"id":"row1","title":"Chart"}}"""))
        assertEquals("Chart", saved.title)
        val saveErr = assertIs<Inbound.ComponentSaveError>(Wire.decode("""{"type":"component_save_error","error":"Component not found"}"""))
        assertEquals("Component not found", saveErr.error)
        val deleted = assertIs<Inbound.ComponentDeleted>(Wire.decode("""{"type":"component_deleted","component_id":"row1"}"""))
        assertEquals("row1", deleted.componentId)
        val status = assertIs<Inbound.CombineStatus>(Wire.decode("""{"type":"combine_status","status":"combining","message":"Combining A with B..."}"""))
        assertEquals("combining", status.status)
        assertEquals("Combining A with B...", status.message)
        val err = assertIs<Inbound.CombineError>(Wire.decode("""{"type":"combine_error","error":"boom"}"""))
        assertEquals("boom", err.error)
        val list = assertIs<Inbound.SavedComponentsList>(Wire.decode("""{"type":"saved_components_list","components":[{"id":"r1"},{"id":"r2"}]}"""))
        assertEquals(2, list.count)
    }

    @Test
    fun decodes_components_combined_rows_with_identity_fallback() {
        val r =
            assertIs<Inbound.ComponentsReplaced>(
                Wire.decode(
                    """{"type":"components_combined","removed_ids":["rowA","rowB"],"new_components":[{"id":"rowC","component_data":{"type":"card","component_id":"wc_1"}},{"id":"rowD","component_data":{"type":"table"}}]}""",
                ),
            )
        assertEquals(listOf("rowA", "rowB"), r.removedIds)
        // The first result carries its stamped workspace identity; the second
        // falls back to the fresh saved-row id.
        assertEquals(listOf("wc_1", "rowD"), r.newComponents.map { it.id })
        assertEquals("card", r.newComponents[0].type)
    }

    @Test
    fun decodes_components_condensed_same_shape() {
        val r =
            assertIs<Inbound.ComponentsReplaced>(
                Wire.decode("""{"type":"components_condensed","removed_ids":["r1","r2","r3"],"new_components":[{"id":"r4","component_data":{"type":"card"}}]}"""),
            )
        assertEquals(3, r.removedIds.size)
        assertEquals(listOf("r4"), r.newComponents.map { it.id })
    }

    @Test
    fun unknown_type_falls_back() {
        val r = assertIs<Inbound.Unknown>(Wire.decode("""{"type":"totally_new_primitive"}"""))
        assertEquals("totally_new_primitive", r.type)
    }

    @Test
    fun malformed_json_is_unknown_not_throw() {
        assertIs<Inbound.Unknown>(Wire.decode("{not valid json"))
    }

    @Test
    fun encodes_register_ui_with_device_caps() {
        val out =
            Wire.encodeRegisterUi(
                token = "TOK",
                sessionId = "chatA",
                device = DeviceCapabilities(screenWidth = 1080, screenHeight = 2340, supportedTypes = listOf("text", "card")),
            )
        val o = Json.parseToJsonElement(out).jsonObject
        assertEquals("register_ui", o["type"]?.jsonPrimitive?.contentOrNull)
        assertEquals("TOK", o["token"]?.jsonPrimitive?.contentOrNull)
        val dev = o["device"]!!.jsonObject
        assertEquals("android", dev["device_type"]?.jsonPrimitive?.contentOrNull)
        assertEquals("1080", dev["screen_width"]?.jsonPrimitive?.contentOrNull)
        assertEquals(2, dev["supported_types"]!!.jsonArray.size)
    }

    @Test
    fun encodes_chat_message() {
        val submission = "0cf52102-cffc-4f3a-9867-8dc744593a55"
        val request = "b1876d0c-7401-47fa-8c78-8cdedba692a8"
        val o =
            Json.parseToJsonElement(
                Wire.encodeChatMessage(
                    message = "hello",
                    chatId = "chatA",
                    requestGeneration = request,
                    submissionId = submission,
                ),
            ).jsonObject
        assertEquals("ui_event", o["type"]?.jsonPrimitive?.contentOrNull)
        assertEquals("chat_message", o["action"]?.jsonPrimitive?.contentOrNull)
        assertEquals("hello", o["payload"]!!.jsonObject["message"]?.jsonPrimitive?.contentOrNull)
        assertEquals("chatA", o["payload"]!!.jsonObject["chat_id"]?.jsonPrimitive?.contentOrNull)
        assertEquals(submission, o["submission_id"]?.jsonPrimitive?.contentOrNull)
        assertEquals(request, o["request_generation"]?.jsonPrimitive?.contentOrNull)
        assertEquals(submission, o["payload"]!!.jsonObject["submission_id"]?.jsonPrimitive?.contentOrNull)
        assertEquals(request, o["payload"]!!.jsonObject["request_generation"]?.jsonPrimitive?.contentOrNull)
    }
}
