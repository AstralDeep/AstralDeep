package com.personalailabs.astraldeep.app

import android.Manifest
import android.content.pm.PackageManager
import androidx.compose.runtime.mutableStateOf
import androidx.compose.ui.semantics.LiveRegionMode
import androidx.compose.ui.semantics.SemanticsProperties
import androidx.compose.ui.test.SemanticsMatcher
import androidx.compose.ui.test.assert
import androidx.compose.ui.test.assertHasClickAction
import androidx.compose.ui.test.assertIsDisplayed
import androidx.compose.ui.test.assertTextContains
import androidx.compose.ui.test.junit4.createComposeRule
import androidx.compose.ui.test.onNodeWithTag
import androidx.compose.ui.test.onNodeWithText
import androidx.compose.ui.test.performClick
import androidx.compose.ui.test.performTextInput
import androidx.test.ext.junit.runners.AndroidJUnit4
import androidx.test.platform.app.InstrumentationRegistry
import com.personalailabs.astraldeep.app.transport.runtimeVoiceCapability
import com.personalailabs.astraldeep.app.ui.InputBar
import com.personalailabs.astraldeep.app.ui.VoiceControlButton
import com.personalailabs.astraldeep.app.ui.VoiceFeedback
import com.personalailabs.astraldeep.app.ui.theme.AstralTheme
import com.personalailabs.astraldeep.app.voice.VoiceTerminalNotice
import com.personalailabs.astraldeep.app.voice.VoiceTerminalNoticeKind
import com.personalailabs.astraldeep.app.voice.VoiceUiState
import com.personalailabs.astraldeep.core.protocol.VoiceControl
import java.security.MessageDigest
import org.junit.Assert.assertEquals
import org.junit.Assert.assertFalse
import org.junit.Assert.assertTrue
import org.junit.Rule
import org.junit.Test
import org.junit.runner.RunWith

/** Connected Android permission/capability and TalkBack evidence for feature 065. */
@RunWith(AndroidJUnit4::class)
class VoiceConversation065InstrumentedTest {
    @get:Rule val rule = createComposeRule()

    private val instrumentation = InstrumentationRegistry.getInstrumentation()
    private val context = instrumentation.targetContext

    @Test
    fun canonicalFixtureIsCopiedIntoTheInstrumentedTestBundle() {
        val bytes =
            instrumentation.context.assets.open("voice_065/client_conformance.json").use {
                it.readBytes()
            }
        val digest =
            MessageDigest.getInstance("SHA-256")
                .digest(bytes)
                .joinToString("") { "%02x".format(it) }

        assertEquals(
            "bc98077594fa8d51dd664fadefaa48cf596a94e7fb2a961a972dbabca4f02143",
            digest,
        )
    }

    @Suppress("DEPRECATION")
    @Test
    fun manifestRequestsOnlyTheRequiredAudioCapturePermission() {
        val requested =
            context.packageManager
                .getPackageInfo(context.packageName, PackageManager.GET_PERMISSIONS)
                .requestedPermissions
                .orEmpty()
                .toSet()

        assertTrue(requested.contains(Manifest.permission.RECORD_AUDIO))
        assertFalse(requested.contains(Manifest.permission.CAMERA))
        assertFalse(requested.contains(Manifest.permission.ACCESS_FINE_LOCATION))
        assertFalse(requested.contains(Manifest.permission.BLUETOOTH_CONNECT))
    }

    @Test
    fun emulatorReportsRealAudioFactsAfterRuntimePermissionGrant() {
        val wasGranted = context.checkSelfPermission(Manifest.permission.RECORD_AUDIO) == PackageManager.PERMISSION_GRANTED
        if (!wasGranted) {
            instrumentation.uiAutomation.grantRuntimePermission(
                context.packageName,
                Manifest.permission.RECORD_AUDIO,
            )
        }
        val capability = runtimeVoiceCapability(context)
        assertTrue(capability.hasMicrophone)
        assertTrue(capability.hasAudioOutput)
        assertEquals("authorized", capability.microphonePermission)
        assertTrue(capability.fullDuplex)
    }

    @Test
    fun serverOwnedVoiceButtonExposesStableAccessibleStateAndAction() {
        var clicked = false
        rule.setContent {
            AstralTheme {
                VoiceControlButton(
                    control =
                        VoiceControl(
                            key = "voice-start",
                            action = "voice_session_start",
                            label = "Start voice conversation",
                            icon = "microphone",
                            visible = true,
                            enabled = true,
                            pressed = false,
                            busy = true,
                        ),
                    phase = "connecting",
                    enabled = true,
                    onClick = { clicked = true },
                )
            }
        }

        rule.onNodeWithTag("voice-control-voice_session_start", useUnmergedTree = true)
            .assertHasClickAction()
            .assert(
                SemanticsMatcher.expectValue(
                    SemanticsProperties.StateDescription,
                    "Start voice conversation, busy, connecting",
                ),
            ).performClick()
        assertTrue(clicked)
    }

    @Test
    fun terminalFailureNoticeIsTextuallyProminentAndAnnouncedAssertively() {
        val notice =
            VoiceTerminalNotice(
                kind = VoiceTerminalNoticeKind.REQUEST_DID_NOT_COMPLETE,
                title = "Voice request didn't complete",
                serverMessage = "The agent could not finish this request.",
                guidance = "Typed chat is still available.",
            )
        rule.setContent {
            AstralTheme {
                VoiceFeedback(
                    VoiceUiState(
                        phase = "listening",
                        reason = "voice_turn_failed",
                        terminalNotice = notice,
                    ),
                )
            }
        }

        rule.onNodeWithTag("voice-terminal-notice")
            .assertIsDisplayed()
            .assert(
                SemanticsMatcher.expectValue(
                    SemanticsProperties.LiveRegion,
                    LiveRegionMode.Assertive,
                ),
            ).assert(
                SemanticsMatcher.expectValue(
                    SemanticsProperties.StateDescription,
                    notice.accessibilityText,
                ),
            )
        rule.onNodeWithText("!", useUnmergedTree = true).assertIsDisplayed()
        rule.onNodeWithText("Voice request didn't complete", useUnmergedTree = true).assertIsDisplayed()
        rule.onNodeWithText("Typed chat is still available.", useUnmergedTree = true).assertIsDisplayed()
    }

    @Test
    fun speechFailureAfterSuccessPresentsTextAvailabilityNotRequestFailure() {
        val notice =
            VoiceTerminalNotice(
                kind = VoiceTerminalNoticeKind.TEXT_RESULT_AVAILABLE,
                title = "Speech playback failed",
                serverMessage = "Request completed. The text result is available in the conversation.",
                guidance = "The request completed. Its committed text result remains available in the conversation.",
                speechUnavailable = true,
            )
        rule.setContent {
            AstralTheme {
                VoiceFeedback(
                    VoiceUiState(
                        phase = "error",
                        reason = "speech_error",
                        terminalNotice = notice,
                    ),
                )
            }
        }

        rule.onNodeWithTag("voice-terminal-notice")
            .assertIsDisplayed()
            .assert(
                SemanticsMatcher.expectValue(
                    SemanticsProperties.StateDescription,
                    notice.accessibilityText,
                ),
            )
        rule.onNodeWithText("!", useUnmergedTree = true).assertIsDisplayed()
        rule.onNodeWithText("Speech playback failed", useUnmergedTree = true).assertIsDisplayed()
        rule.onNodeWithText(
            "The request completed. Its committed text result remains available in the conversation.",
            useUnmergedTree = true,
        ).assertIsDisplayed()
    }

    @Test
    fun terminalVoiceNoticeDoesNotClearTypedComposerText() {
        val voiceState = mutableStateOf(VoiceUiState(phase = "listening"))
        rule.setContent {
            AstralTheme {
                InputBar(
                    staged = emptyList(),
                    readOnly = false,
                    voice = voiceState.value,
                    onVoiceControl = { _, _ -> },
                    onSend = {},
                    onStageFile = { _, _, _ -> },
                    onRemoveAttachment = {},
                    onOpenAttachments = {},
                )
            }
        }

        rule.onNodeWithTag("chat-input").performTextInput("keep this typed request")
        rule.runOnIdle {
            voiceState.value =
                voiceState.value.copy(
                    reason = "voice_turn_refused",
                    terminalNotice =
                        VoiceTerminalNotice(
                            kind = VoiceTerminalNoticeKind.REQUEST_DID_NOT_START,
                            title = "Voice request didn't start",
                            serverMessage = "The request was refused.",
                            guidance =
                                "Retry requires a new explicit spoken request. " +
                                    "Typed chat is still available.",
                            retryPolicy = "explicit_user_retry",
                        ),
                )
        }

        rule.onNodeWithTag("chat-input").assertTextContains("keep this typed request")
        rule.onNodeWithText("Voice request didn't start", useUnmergedTree = true).assertIsDisplayed()
        rule.onNodeWithText(
            "Retry requires a new explicit spoken request. Typed chat is still available.",
            useUnmergedTree = true,
        ).assertIsDisplayed()
    }
}
