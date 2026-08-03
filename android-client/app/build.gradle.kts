// :app — the Android/Compose client. Depends on :core for all pure logic.
import org.gradle.api.DefaultTask
import org.gradle.api.file.RegularFileProperty
import org.gradle.api.provider.Property
import org.gradle.api.tasks.CacheableTask
import org.gradle.api.tasks.Input
import org.gradle.api.tasks.InputFile
import org.gradle.api.tasks.OutputFile
import org.gradle.api.tasks.PathSensitive
import org.gradle.api.tasks.PathSensitivity
import org.gradle.api.tasks.TaskAction
import java.nio.file.Files
import java.nio.file.StandardCopyOption
import java.security.MessageDigest
import java.util.Properties

plugins {
    alias(libs.plugins.android.application)
    alias(libs.plugins.kotlin.serialization)
    alias(libs.plugins.compose.compiler)
    alias(libs.plugins.kover)
    alias(libs.plugins.ktlint)
}

@CacheableTask
abstract class CopyCanonicalVoiceFixture065Task : DefaultTask() {
    @get:InputFile
    @get:PathSensitive(PathSensitivity.RELATIVE)
    abstract val sourceFixture: RegularFileProperty

    @get:Input
    abstract val expectedSha256: Property<String>

    @get:OutputFile
    abstract val unitTestFixture: RegularFileProperty

    @get:OutputFile
    abstract val instrumentedTestFixture: RegularFileProperty

    @TaskAction
    fun copyAndVerify() {
        val source = sourceFixture.get().asFile
        require(source.isFile) { "canonical Feature 065 voice fixture is missing" }
        val canonicalBytes = source.readBytes()
        val sourceDigest = sha256(canonicalBytes)
        require(sourceDigest == expectedSha256.get()) {
            "canonical Feature 065 voice fixture digest changed: $sourceDigest"
        }
        for (output in listOf(unitTestFixture, instrumentedTestFixture)) {
            val destination = output.get().asFile
            Files.createDirectories(destination.parentFile.toPath())
            val temporary = destination.resolveSibling(".${destination.name}.tmp")
            Files.write(temporary.toPath(), canonicalBytes)
            Files.move(
                temporary.toPath(),
                destination.toPath(),
                StandardCopyOption.REPLACE_EXISTING,
            )
            require(sha256(destination.readBytes()) == sourceDigest) {
                "Feature 065 bundled fixture differs from its canonical source"
            }
        }
    }

    private fun sha256(bytes: ByteArray): String =
        MessageDigest.getInstance("SHA-256")
            .digest(bytes)
            .joinToString("") { "%02x".format(it) }
}

// Release signing is read from a gitignored keystore.properties (see docs/play-store-release.md).
// Absent on CI and fresh clones — release builds are simply unsigned there.
val keystoreProperties =
    Properties().apply {
        val f = rootProject.file("keystore.properties")
        if (f.exists()) f.inputStream().use { load(it) }
    }

// Feature 065 has one canonical C0-C6 fixture at the repository root. Android
// test resources/assets are generated from those exact bytes; no client-owned
// JSON variant is kept in this project.
val canonicalVoiceFixture =
    rootProject.layout.projectDirectory.file(
        "../backend/tests/fixtures/voice_065/client_conformance.json",
    )
val canonicalVoiceFixtureSha256 =
    "bc98077594fa8d51dd664fadefaa48cf596a94e7fb2a961a972dbabca4f02143"
val voiceFixtureUnitOutput =
    layout.buildDirectory.file(
        "generated/voice-fixture-065/testResources/voice_065/client_conformance.json",
    )
val voiceFixtureAndroidTestOutput =
    layout.buildDirectory.file(
        "generated/voice-fixture-065/androidTestAssets/voice_065/client_conformance.json",
    )

val copyCanonicalVoiceFixture065 =
    tasks.register<CopyCanonicalVoiceFixture065Task>("copyCanonicalVoiceFixture065") {
        group = "verification"
        description = "Hash-check and copy the canonical Feature 065 fixture into test bundles"
        sourceFixture.set(canonicalVoiceFixture)
        expectedSha256.set(canonicalVoiceFixtureSha256)
        unitTestFixture.set(voiceFixtureUnitOutput)
        instrumentedTestFixture.set(voiceFixtureAndroidTestOutput)
    }

android {
    namespace = "com.personalailabs.astraldeep.app"
    compileSdk = libs.versions.compileSdk.get().toInt()

    defaultConfig {
        // Play Store identity (registered; permanent once uploaded). The Kotlin
        // namespace, source packages, and the AppAuth redirect scheme all share
        // this id — the scheme must match the astral-mobile client's Valid
        // Redirect URI in Keycloak.
        applicationId = "com.personalailabs.astraldeep"
        minSdk = libs.versions.minSdk.get().toInt()
        targetSdk = libs.versions.targetSdk.get().toInt()
        versionCode = 4
        versionName = "1.2"
        testInstrumentationRunner = "androidx.test.runner.AndroidJUnitRunner"

        // AppAuth captures the OIDC redirect via this scheme (RedirectUriReceiverActivity).
        // Must match the astral-mobile client's Valid Redirect URI:
        //   com.personalailabs.astraldeep:/oauth2redirect
        manifestPlaceholders["appAuthRedirectScheme"] = "com.personalailabs.astraldeep"
    }

    buildFeatures {
        compose = true
        buildConfig = true
    }

    signingConfigs {
        if (keystoreProperties.isNotEmpty()) {
            create("release") {
                storeFile = file(keystoreProperties.getProperty("storeFile"))
                storePassword = keystoreProperties.getProperty("storePassword")
                keyAlias = keystoreProperties.getProperty("keyAlias")
                keyPassword = keystoreProperties.getProperty("keyPassword")
            }
        }
    }

    buildTypes {
        debug {
            isMinifyEnabled = false
        }
        release {
            isMinifyEnabled = false
            proguardFiles(getDefaultProguardFile("proguard-android-optimize.txt"), "proguard-rules.pro")
            signingConfig = signingConfigs.findByName("release")
        }
    }

    compileOptions {
        sourceCompatibility = JavaVersion.VERSION_17
        targetCompatibility = JavaVersion.VERSION_17
    }
    kotlin {
        jvmToolchain(17)
    }

    testOptions {
        unitTests.isReturnDefaultValues = true
    }

    sourceSets {
        getByName("test").resources.directories.add(
            "build/generated/voice-fixture-065/testResources",
        )
        getByName("androidTest").assets.directories.add(
            "build/generated/voice-fixture-065/androidTestAssets",
        )
    }
}

tasks.configureEach {
    if (
        name.startsWith("test") ||
        name.contains("AndroidTest") ||
        name.startsWith("kover") ||
        (name.startsWith("process") && name.endsWith("UnitTestJavaRes"))
    ) {
        dependsOn(copyCanonicalVoiceFixture065)
    }
}

composeCompiler {
    // :core is pure Kotlin (no Compose dep), so its wire types are declared
    // stable via this config instead of @Immutable annotations (feature 052).
    stabilityConfigurationFiles.add(rootProject.layout.projectDirectory.file("compose_stability.conf"))
    metricsDestination = layout.buildDirectory.dir("compose-metrics")
    reportsDestination = layout.buildDirectory.dir("compose-reports")
}

dependencies {
    implementation(projects.core)

    implementation(platform(libs.compose.bom))
    implementation(libs.compose.ui)
    implementation(libs.compose.ui.graphics)
    implementation(libs.compose.ui.tooling.preview)
    debugImplementation(libs.compose.ui.tooling)
    implementation(libs.compose.material3)
    implementation(libs.compose.material3.adaptive)
    implementation(libs.compose.material3.adaptive.layout)
    implementation(libs.compose.material3.adaptive.navigation)

    implementation(libs.androidx.core.ktx)
    implementation(libs.androidx.activity.compose)
    implementation(libs.androidx.lifecycle.viewmodel.compose)
    implementation(libs.androidx.lifecycle.runtime.compose)
    implementation(libs.androidx.window)
    implementation(libs.androidx.security.crypto)
    implementation(libs.androidx.datastore.preferences)

    implementation(libs.kotlinx.coroutines.android)
    implementation(libs.kotlinx.serialization.json)
    implementation(libs.okhttp)
    implementation(libs.appauth)
    implementation(libs.coil.compose)
    implementation(libs.livekit.android)
    // LiveKit 2.27.0 publishes vulnerable protobuf-javalite 3.22.0. The
    // catalog's strict 3.25.5 constraint is the approved compatible repair
    // for CVE-2024-7254/GHSA-735f-pc8j-v9w8; the generated lock pins it.
    implementation(libs.protobuf.javalite)

    testImplementation(libs.junit)
    testImplementation(libs.kotlin.test.junit)
    testImplementation(libs.kotlinx.coroutines.test)
    testImplementation(libs.okhttp.mockwebserver)

    androidTestImplementation(libs.androidx.test.ext.junit)
    androidTestImplementation(libs.espresso.core)
    androidTestImplementation(platform(libs.compose.bom))
    androidTestImplementation(libs.compose.ui.test.junit4)
    debugImplementation(libs.compose.ui.test.manifest)
}
