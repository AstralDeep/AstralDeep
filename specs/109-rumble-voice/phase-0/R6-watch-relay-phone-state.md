# R6: what the paired-watch relay needs from the phone app

**Question.** Does the paired-watch relay keep working when the phone app is in the background, given that the new models use the GPU?

**Answer.** No, and it does not work in the background today either. A watch voice session needs the phone app in front, unlocked, with the mesh open. The code enforces this on purpose. The new runtime does not change that rule. It changes what a violation does: llama.cpp with Metal fails or aborts on background GPU work, where the current runtime was simply never allowed to start. Recommendation: keep "phone app in front" as the stated limit and make the failure explicit, and keep the CPU path for a later decision.

All code paths below are **read** at Projection `54e7ff1` under `apple-clients/`. Nothing here was run. Whether the behavior matches on hardware is listed at the end.

## What the phone must be doing today

A session starts when the watch sends a `start` packet over `sendMessageData`. The phone handles it in `AstralApp/AstralApp/WatchOverrideSync.swift:92-117` and `Voice/PairedWatchVoiceRelay.swift:93-131`. Four conditions must hold.

1. **The relay is bound.** `model.watchVoice.bind(prepare:isCurrent:)` runs in a `.task` of `Mesh/MeshRootView.swift:160`. Without it `prepare` is nil and the start is rejected (`PairedWatchVoiceRelay.swift:99-101`). The mesh opens only while the scene is active and protected data is available (`Mesh/MeshRootView.swift:67-72`). A phone process that was only woken by WatchConnectivity never reaches that code, so it has no bound relay. This is inferred from the code and is not observed.
2. **The voice controller is eligible.** `OnDeviceVoiceController.swift:50` defines `eligible` as `foreground && !interrupted && !locked`. `start` refuses when it is false and sets the message "Return to this app, then start voice again." (`:84-93`). The relay then reports `phone_voice_unavailable` to the watch (`PairedWatchVoiceRelay.swift:263-266`).
3. **The scene is active.** `AstralAppMain.swift:134-138` maps `.inactive` and `.background` to `voiceSceneBecameInactive`, which calls `onDeviceVoice.suspend()` (`AppModel.swift:101-103`). `suspend()` clears `foreground` and calls `stop()` (`OnDeviceVoiceController.swift:211-214`). `stop()` fires `onSessionStopped`, and the relay answers by sending `stop` to the watch (`PairedWatchVoiceRelay.swift:47-49, 64-91`).
4. **The phone is unlocked.** `AstralAppMain.swift:175-177` calls `voiceSessionLocked` on `protectedDataWillBecomeUnavailable`. That sets `locked` and stops the session (`OnDeviceVoiceController.swift:223-226`).

`ONDEVICE-VOICE.md:105-109` states the same rule in prose: "Backgrounding, owner or conversation changes, loss of reachability and Stop retire the session; returning does not restart it."

No test covers a watch session meeting a backgrounded phone. `OnDeviceVoiceControllerTests.swift:95-110` covers `suspend()` on a phone-microphone session only. `PairedWatchVoiceTests` has no scene or lock case.

## Reachability, start and stop

The phone calls `WatchOverrideSync.shared.activate()` when `AppModel` is created (`AppModel.swift:81`). `reachable` needs an activated session, a paired watch, an installed watch app and `isReachable` (`WatchOverrideSync.swift:18-23`). Losing reachability calls `onReachabilityChanged(false)` and the relay stops the session (`PairedWatchVoiceRelay.swift:46`). A session also ends after 3,600 s (`:199-202`) and a start that is not admitted ends after 90 s (`:106-109`).

Apple's rules (**read**, developer.apple.com):

- `WCSession.isReachable`: on the phone it is true when a paired watch is in range, the watch extension is running and its own `isReachable` is true. On the watch it is true while the watch app runs in the foreground or at high priority in the background. <https://developer.apple.com/documentation/watchconnectivity/wcsession/isreachable>
- `sendMessageData`: a call from the active watch app "wakes up the corresponding iOS app in the background and makes it reachable". A call from the phone does not wake the watch app. <https://developer.apple.com/documentation/watchconnectivity/wcsession/sendmessagedata(_:replyhandler:errorhandler:)>

So the watch can wake the phone process. The phone app's own rules then refuse the session, because conditions 1 to 4 above fail. The watch side agrees: `WatchVoiceBridge.swift:200-203` ends the session when the watch app leaves the foreground, and `WatchOverrideSync.swift:22` needs the phone to be reachable.

## Declared background modes and entitlements

- `AstralApp/Info.plist:30-33`: `UIBackgroundModes` holds `remote-notification` only. No `audio`, `voip`, `processing` or `bluetooth` mode.
- `AstralApp/AstralApp-iOS.entitlements`: `aps-environment` and `com.apple.developer.associated-domains`. No `increased-memory-limit`, and no `com.apple.developer.background-tasks.continued-processing.gpu`.
- The phone runs no `AVAudioSession` for a watch session. `PairedWatchVoiceAudio.swift` takes PCM from packets and sends PCM back, so the phone neither records nor plays. Only the phone-microphone path (`OnDevice/VoiceDuplexEngine.swift:121-124`) sets a session, and that is `playAndRecord` with no background mode behind it.
- The watch `WatchInfo.plist` declares no background mode. The watch bridge is foreground only.
- The iOS deployment target is 17.0 (`AstralApp.xcodeproj/project.pbxproj:610`).

## What the new runtime changes

The current runtime is also GPU-bound. `AstralVoiceModel` is MLX Swift, whose Metal shaders are called out in `AstralVoiceModel/Package.swift:3`. So the background limit applies to today's model as well. It has stayed hidden because the controller refuses to start outside the foreground.

Apple (**read**): "iOS and tvOS restrict a background app's access to the GPU". New command buffers from a background app are not scheduled. They complete with `MTLCommandBufferError.Code.notPermitted`. <https://developer.apple.com/documentation/metal/preparing-your-metal-app-to-run-in-the-background>

llama.cpp (**read**, GitHub):

- Issue 16998, opened 4 November 2025 against build b6940 on iOS 18.7.1: minimizing the app during `llama_decode` gave `command buffer 0 failed with status 5`, "Insufficient Permission (to submit GPU work from background)", then a fatal abort in `ggml-metal-context.m`. <https://github.com/ggml-org/llama.cpp/issues/16998>
- Pull request 20306, merged 10 March 2026 (commit `1a5631b`): replaces the abort with a sticky error flag. Later `graph_compute` calls return `GGML_STATUS_FAILED`, and the backend must be recreated. <https://github.com/ggml-org/llama.cpp/pull/20306>

R2 pins the llama.cpp revision. It must include 20306, or a backgrounded phone with an open watch session can crash the app. Neither the pilot nor the lab vendors a copy of `ggml-metal-context.m` that I could inspect (`~/Desktop/VoiceTesting/Vendor` is empty), so I cannot say which side of the fix the pilot was on.

## Options

| Option | Fit | Notes |
|---|---|---|
| **Phone app in front, unlocked, mesh open** | Matches today. No new capability. | The watch notice must say so. Needs the runtime to stop GPU work on `.inactive`, before the system revokes it, and to drop to a clean "phone not ready" failure. |
| **CPU-only llama.cpp while backgrounded** | Technically possible: set GPU layers to zero and recreate the backend. | Speed is unknown. The pilot's Rumble recognition runs on Metal on an A15. Also needs a wake path that opens the mesh with no scene, which the app does not have, and that is a security design change, not a runtime one. Not recommended in this spec. |
| **`audio` background mode** | Poor. | It is for apps that play audible content. The relay plays and records nothing on the phone. It would not lift the GPU rule. Apple's background-modes page lists `audio` as "plays audible content in the background". Declaring it with no real audio is a review risk. <https://developer.apple.com/documentation/xcode/configuring-background-execution-modes> |
| **`BGContinuedProcessingTask` with Background GPU Access** | Poor. | iOS 26 and later only, and the app supports 17. It must start in the foreground from a person's action and shows a Live Activity the person can cancel. A watch-initiated session with the phone already backgrounded cannot satisfy "starts in the foreground". <https://developer.apple.com/documentation/backgroundtasks/performing-long-running-tasks-on-ios-and-ipados> |

**Recommendation.** Keep the rule as it is and state it. Spoken limit: the watch voice session needs the phone app open and unlocked. The runtime stops GPU work when the scene goes inactive and ends the session with the existing stop path. Spec section 12 already says the phone must be reachable. It should add "and in front". Do not add background modes.

## Still unknown, hardware only

- How long a `sendMessageData` wake keeps the phone process running, and whether `AppModel` activation is reached on a cold wake. This decides the exact failure the watch sees today.
- Whether an in-flight llama.cpp decode survives the `.inactive` to `.background` transition on the pinned revision, with and without 20306.
- The speed of Rumble on CPU on an A15, if the CPU option is ever wanted. R1 measures the GPU path only.
- Behavior with the phone locked and the screen on, and with Control Center or a call banner over the app. Both give `.inactive`, and both stop the session today.
