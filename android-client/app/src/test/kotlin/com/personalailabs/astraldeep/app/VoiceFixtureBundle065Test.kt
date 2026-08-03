package com.personalailabs.astraldeep.app

import java.security.MessageDigest
import kotlin.test.Test
import kotlin.test.assertEquals
import kotlin.test.assertNotNull

/** Proves the JVM test bundle contains the hash-checked canonical C0-C6 bytes. */
class VoiceFixtureBundle065Test {
    @Test
    fun canonicalFixtureIsCopiedIntoTheUnitTestBundle() {
        val stream =
            assertNotNull(
                javaClass.classLoader?.getResourceAsStream(
                    "voice_065/client_conformance.json",
                ),
            )
        val bytes = stream.use { it.readBytes() }

        assertEquals(CANONICAL_FIXTURE_SHA256, sha256(bytes))
    }

    private fun sha256(bytes: ByteArray): String =
        MessageDigest.getInstance("SHA-256")
            .digest(bytes)
            .joinToString("") { "%02x".format(it) }

    private companion object {
        const val CANONICAL_FIXTURE_SHA256 =
            "bc98077594fa8d51dd664fadefaa48cf596a94e7fb2a961a972dbabca4f02143"
    }
}
