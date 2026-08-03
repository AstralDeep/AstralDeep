package com.personalailabs.astraldeep.app.ui

import com.personalailabs.astraldeep.app.rest.AstralRest
import com.personalailabs.astraldeep.app.transport.OrchestratorClient
import com.personalailabs.astraldeep.core.protocol.Inbound
import kotlin.test.Test
import kotlin.test.assertEquals
import kotlin.test.assertFalse
import kotlin.test.assertNull
import kotlin.test.assertTrue

/** Feature 044 US1 — the reducer's error/progress/notification behavior (T012/T021). */
class AppViewModelReducerTest {
    private val vm = AppViewModel(OrchestratorClient("ws://localhost:9/ws"), AstralRest("http://localhost:9"))

    /** A turn in flight — the state an error must fully resolve (SC-006). */
    private val midTurn =
        UiState(
            turnActive = true,
            pendingReplace = true,
            statusText = "Thinking…",
            stepTrail = listOf("• web_search"),
        )

    @Test
    fun error_frame_sets_banner_and_resolves_the_turn() {
        val s = vm.reduce(midTurn, Inbound.ErrorFrame(code = "forbidden", message = "Nope"))
        assertEquals("Nope (forbidden)", s.banner)
        assertEquals("error", s.bannerKind)
        assertFalse(s.turnActive)
        assertFalse(s.pendingReplace)
        assertNull(s.statusText)
    }

    @Test
    fun error_frame_internal_code_is_not_appended() {
        val s = vm.reduce(UiState(), Inbound.ErrorFrame(code = "internal", message = "boom"))
        assertEquals("boom", s.banner)
    }

    @Test
    fun chat_step_appends_then_updates_in_place() {
        var s = vm.reduce(UiState(), Inbound.ChatStep(id = "s1", name = "web_search", status = "running"))
        assertEquals(listOf("• web_search"), s.stepTrail)
        s = vm.reduce(s, Inbound.ChatStep(id = "s1", name = "web_search", status = "completed"))
        assertEquals(listOf("✓ web_search"), s.stepTrail)
        s = vm.reduce(s, Inbound.ChatStep(id = "s2", name = "fetch_page", status = "errored"))
        assertEquals(listOf("✓ web_search", "✗ fetch_page"), s.stepTrail)
    }

    @Test
    fun tool_progress_percent_ticks_update_in_place() {
        var s = vm.reduce(UiState(), Inbound.ToolProgress("web_search: fetching results (20%)"))
        s = vm.reduce(s, Inbound.ToolProgress("web_search: fetching results (60%)"))
        assertEquals(listOf("• web_search: fetching results (60%)"), s.stepTrail)
    }

    @Test
    fun step_trail_is_capped() {
        var s = UiState()
        repeat(25) { i -> s = vm.reduce(s, Inbound.ChatStep(id = "s$i", name = "tool_$i", status = "completed")) }
        assertEquals(20, s.stepTrail.size)
        assertEquals("✓ tool_24", s.stepTrail.last())
    }

    @Test
    fun task_started_relaxes_into_background() {
        val s = vm.reduce(midTurn, Inbound.TaskStarted("t1"))
        assertTrue(s.turnActive)
        assertTrue(s.asyncDetached)
        assertEquals("Working in the background…", s.statusText)
    }

    @Test
    fun task_completed_resolves_the_turn_and_banners() {
        val s = vm.reduce(midTurn.copy(asyncDetached = true), Inbound.TaskCompleted("t1", "c1"))
        assertFalse(s.turnActive)
        assertFalse(s.pendingReplace)
        assertFalse(s.asyncDetached)
        assertEquals("Background task finished", s.banner)
        assertEquals("info", s.bannerKind)
    }

    @Test
    fun idle_completed_and_unknown_chat_statuses_cannot_leave_first_load_working_chrome() {
        val stale = UiState(statusText = "Completed")

        val idle = vm.reduce(stale, Inbound.ChatStatus(status = "idle", message = "Completed"))
        assertFalse(idle.turnActive)
        assertNull(idle.statusText)
        assertNull(idle.workingStatusText)

        val completed = vm.reduce(stale, Inbound.ChatStatus(status = "completed", message = "Completed"))
        assertFalse(completed.turnActive)
        assertNull(completed.statusText)
        assertNull(completed.workingStatusText)

        val unknown = vm.reduce(stale, Inbound.ChatStatus(status = "mystery", message = "Completed"))
        assertFalse(unknown.hasActiveWork)
        assertNull(unknown.statusText)
        assertNull(unknown.workingStatusText)
    }

    @Test
    fun informational_chat_status_uses_a_banner_instead_of_the_working_row() {
        val s = vm.reduce(UiState(), Inbound.ChatStatus(status = "info", message = "Attachment ready"))
        assertFalse(s.turnActive)
        assertNull(s.statusText)
        assertNull(s.workingStatusText)
        assertEquals("Attachment ready", s.banner)
        assertEquals("info", s.bannerKind)
    }

    @Test
    fun notification_sets_banner_with_title_prefix_and_level_styling() {
        val info = vm.reduce(UiState(), Inbound.Notification(title = "Daily brief", body = "Ready", level = "info"))
        assertEquals("Daily brief: Ready", info.banner)
        assertEquals("info", info.bannerKind)
        val err = vm.reduce(UiState(), Inbound.Notification(title = null, body = "Job failed", level = "error"))
        assertEquals("Job failed", err.banner)
        assertEquals("error", err.bannerKind)
    }

    @Test
    fun unknown_frame_is_a_state_noop() {
        assertEquals(midTurn, vm.reduce(midTurn, Inbound.Unknown("mystery_frame")))
        // A classified-ignored type (parity matrix) is also a quiet state noop.
        assertEquals(midTurn, vm.reduce(midTurn, Inbound.Unknown("heartbeat")))
    }
}
