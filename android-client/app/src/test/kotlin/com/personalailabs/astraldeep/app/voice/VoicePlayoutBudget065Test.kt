package com.personalailabs.astraldeep.app.voice

import kotlin.test.Test
import kotlin.test.assertEquals
import kotlin.test.assertNull

class VoicePlayoutBudget065Test {
    @Test
    fun exact24KhzBudgetHardStopsTheLastFrame() {
        assertEquals(480, boundedPcmFrameCount(24_000, 24_000, 480))
        assertEquals(120, boundedPcmFrameCount(120, 24_000, 480))
    }

    @Test
    fun rtc48KhzFramesUseEquivalentDeclaredDurationWithoutFractionalSamples() {
        assertEquals(960, boundedPcmFrameCount(24_000, 48_000, 960))
        assertEquals(240, boundedPcmFrameCount(120, 48_000, 960))
        assertEquals(478, boundedPcmFrameCount(1_000, 48_000, 479))
    }

    @Test
    fun unsupportedRatesAndEmptyBudgetsFailClosed() {
        assertNull(boundedPcmFrameCount(120, 44_100, 441))
        assertNull(boundedPcmFrameCount(120, 96_000, 960))
        assertNull(boundedPcmFrameCount(0, 24_000, 480))
        assertNull(boundedPcmFrameCount(120, 24_000, 0))
    }
}
