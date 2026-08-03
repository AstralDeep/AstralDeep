pluginManagement {
    repositories {
        google()
        mavenCentral()
        gradlePluginPortal()
    }
}
// Spec 060 T126 (Constitution V): lock the settings plugin classpath (the
// foojay resolver below) too — writes settings-gradle.lockfile at the root.
// Must run BEFORE the plugins {} block resolves that classpath.
buildscript {
    configurations.getByName("classpath") {
        resolutionStrategy.activateDependencyLocking()
    }
}

plugins {
    id("org.gradle.toolchains.foojay-resolver-convention") version "1.0.0"
}

dependencyResolutionManagement {
    repositoriesMode.set(RepositoriesMode.FAIL_ON_PROJECT_REPOS)
    repositories {
        google()
        mavenCentral()
        maven {
            url = uri("https://jitpack.io")
            content {
                // LiveKit's exact AudioSwitch commit is its sole JitPack
                // transitive. Never expose this repository to other groups.
                includeModule("com.github.davidliu", "audioswitch")
            }
        }
    }
}

rootProject.name = "astral-android"
enableFeaturePreview("TYPESAFE_PROJECT_ACCESSORS")
include(":core", ":app")
