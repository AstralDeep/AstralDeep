package com.personalailabs.astraldeep.app.transport

import android.Manifest
import android.content.Context
import android.content.pm.PackageManager
import android.media.AudioManager
import androidx.core.content.ContextCompat
import com.personalailabs.astraldeep.core.protocol.DeviceCapabilities
import java.util.UUID

data class RuntimeVoiceCapability(
    val hasMicrophone: Boolean,
    val hasAudioOutput: Boolean,
    val microphonePermission: String,
    val fullDuplex: Boolean,
)

/**
 * Build the [DeviceCapabilities] reported in `register_ui`. Pure (takes raw
 * metrics) so it is JVM-unit-testable; the Activity supplies real screen metrics
 * and the renderer registry supplies [supportedTypes] (the natively-renderable
 * primitive set ROTE negotiates against). `device_type` is always "android".
 */
fun deviceCapabilities(
    widthPx: Int,
    heightPx: Int,
    pixelRatio: Double,
    supportedTypes: List<String>,
    deviceId: String? = null,
    voice: RuntimeVoiceCapability = RuntimeVoiceCapability(false, false, "not_determined", false),
): DeviceCapabilities =
    DeviceCapabilities(
        screenWidth = widthPx,
        screenHeight = heightPx,
        viewportWidth = widthPx,
        viewportHeight = heightPx,
        pixelRatio = pixelRatio,
        hasTouch = true,
        supportedTypes = supportedTypes,
        deviceType = "android",
        deviceId = deviceId,
        hasMicrophone = voice.hasMicrophone,
        hasAudioOutput = voice.hasAudioOutput,
        microphonePermission = voice.microphonePermission,
        fullDuplex = voice.fullDuplex,
        voiceTransport = "livekit",
    )

/** Stable non-secret installation identity used only with a live server binding. */
fun voiceDeviceId(context: Context): String {
    val preferences = context.getSharedPreferences(VOICE_PREFERENCES, Context.MODE_PRIVATE)
    val existing = preferences.getString(DEVICE_ID, null)
    if (existing != null && isCanonicalUuid4(existing)) return existing
    val generated = UUID.randomUUID().toString()
    preferences.edit().putString(DEVICE_ID, generated).apply()
    return generated
}

/** Record an actual runtime prompt so a later denial is not reported as unasked. */
fun markMicrophonePermissionRequested(context: Context) {
    context.getSharedPreferences(VOICE_PREFERENCES, Context.MODE_PRIVATE)
        .edit()
        .putBoolean(MICROPHONE_REQUESTED, true)
        .apply()
}

/** Runtime capture/playback facts; no camera/Bluetooth/location permission is inferred. */
fun runtimeVoiceCapability(context: Context): RuntimeVoiceCapability {
    val hasMicrophone = context.packageManager.hasSystemFeature(PackageManager.FEATURE_MICROPHONE)
    val audio = context.getSystemService(Context.AUDIO_SERVICE) as? AudioManager
    val hasAudioOutput = audio?.getDevices(AudioManager.GET_DEVICES_OUTPUTS)?.isNotEmpty() == true
    val authorized = ContextCompat.checkSelfPermission(context, Manifest.permission.RECORD_AUDIO) == PackageManager.PERMISSION_GRANTED
    val requested =
        context.getSharedPreferences(VOICE_PREFERENCES, Context.MODE_PRIVATE)
            .getBoolean(MICROPHONE_REQUESTED, false)
    val permission =
        when {
            authorized -> "authorized"
            requested -> "denied"
            else -> "not_determined"
        }
    return RuntimeVoiceCapability(
        hasMicrophone = hasMicrophone,
        hasAudioOutput = hasAudioOutput,
        microphonePermission = permission,
        fullDuplex = hasMicrophone && hasAudioOutput,
    )
}

private fun isCanonicalUuid4(value: String): Boolean {
    val parsed = runCatching { UUID.fromString(value) }.getOrNull()
    return parsed?.version() == 4 && parsed.toString() == value
}

private const val VOICE_PREFERENCES = "astraldeep_voice_device"
private const val DEVICE_ID = "device_id"
private const val MICROPHONE_REQUESTED = "microphone_requested"
