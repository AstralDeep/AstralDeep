package com.personalailabs.astraldeep.app.voice

import java.io.File
import kotlin.test.Test
import kotlin.test.assertFalse
import kotlin.test.assertTrue

/** Locks the local-development cleartext exception to debug packaging only. */
class VoiceDebugNetworkPolicy065Test {
    @Test
    fun debugAllowsLanMediaWhileTheReleaseBaseManifestRetainsPlatformTlsDefaults() {
        val debugManifest = File("src/debug/AndroidManifest.xml").readText()
        val debugPolicy = File("src/debug/res/xml/network_security_config.xml").readText()
        val baseManifest = File("src/main/AndroidManifest.xml").readText()

        assertTrue(debugManifest.contains("android:networkSecurityConfig=\"@xml/network_security_config\""))
        assertTrue(debugPolicy.contains("<base-config cleartextTrafficPermitted=\"true\" />"))
        assertFalse(baseManifest.contains("networkSecurityConfig"))
        assertFalse(baseManifest.contains("usesCleartextTraffic"))
    }

    @Test
    fun mediaFailureDiagnosticNeverIncludesThrowableMessages() {
        val secret = "credentialed-url-and-token"
        val diagnostic =
            redactedMediaFailureType(
                IllegalStateException(secret, java.net.UnknownServiceException(secret)),
            )

        assertTrue(diagnostic.contains("IllegalStateException"))
        assertTrue(diagnostic.contains("UnknownServiceException"))
        assertFalse(diagnostic.contains(secret))
    }
}
