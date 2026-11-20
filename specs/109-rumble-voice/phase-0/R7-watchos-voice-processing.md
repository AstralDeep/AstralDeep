# R7: voice processing on watchOS

**Question.** Does `AVAudioInputNode.setVoiceProcessingEnabled(_:)` work on watchOS, so the watch could listen while it speaks?

**Answer.** The API is declared for watchOS 6.0 and later and compiles for the app's watchOS 10 target. Whether it succeeds on a watch and cancels echo there is not shown by any header or document, and only a watch can settle it. Decision C13 stands either way: the watch stays half duplex with tap to interrupt in this spec.

## Evidence

**Header (read).** Xcode 27.2, WatchOS27.2 SDK:

`/Applications/Xcode.app/Contents/Developer/Platforms/WatchOS.platform/Developer/SDKs/WatchOS27.2.sdk/System/Library/Frameworks/AVFAudio.framework/Headers/AVAudioIONode.h`

- Line 164: `- (BOOL)setVoiceProcessingEnabled:(BOOL)enabled error:(NSError **)outError API_AVAILABLE(macos(10.15), ios(13.0), watchos(6.0), tvos(13.0));`
- Line 139: `voiceProcessingEnabled` property, same availability.
- Lines 226, 235, 243: `voiceProcessingBypassed`, `voiceProcessingAGCEnabled` and `voiceProcessingInputMuted` are also `watchos(6.0)`.
- The class itself, `AVAudioIONode`, is `watchos(2.0)` (line 113).
- Not available on watchOS: `voiceProcessingOtherAudioDuckingConfiguration` (line 267, `API_UNAVAILABLE(watchos, tvos)`), its level enum (line 80) and struct (lines 92 and 95), `setMutedSpeechActivityEventListener` (line 257) and the speech activity event enum (line 64).
- The header's own discussion (lines 141-163): voice processing needs both input and output nodes in the mode, works only when the engine renders to the device and not in manual rendering, and can be changed only while the engine is stopped.

**Documentation (read).** Apple lists `setVoiceProcessingEnabled(_:)` as "watchOS 6.0+". <https://developer.apple.com/documentation/avfaudio/avaudioionode/setvoiceprocessingenabled(_:)>. The page says nothing about runtime support or echo cancellation quality on the watch.

**Compile check (measured by me, this machine).** A three-line file calling `engine.inputNode.setVoiceProcessingEnabled(true)` type-checked with `swiftc -typecheck` against the WatchOS27.2 SDK at `-target arm64_32-apple-watchos10.0` with no errors. The same file setting `voiceProcessingOtherAudioDuckingConfiguration` failed with "is unavailable in watchOS". The test files were in the scratch folder and nothing was run on a device or simulator. A compile proves availability only.

**What the watch does today (read).** `AstralWatch/WatchVoiceBridge.swift`:

- Capture and playback share one `AVAudioEngine` (`:400-401`). The session is `playAndRecord` with mode `voiceChat` and option `duckOthers` when capturing (`:482-485`). Nothing calls `setVoiceProcessingEnabled`. A search of `AstralWatch/` for `voiceProcessing` finds nothing.
- Capture is a tap on the input node at the hardware format, converted to 16 kHz mono (`:490-504, 558-590`).
- The watch drops capture when speech starts: the `speechStart` handler calls `stopCapture()` (`:290`), and `playbackFinished` turns capture back on (`:334-341`). That is the half duplex behavior in the spec.
- Tap to interrupt: `interruptPlayback()` sends `stopSpeech` to the phone (`:344-355`).
- Any foreground loss ends the session (`:200-203`).

The phone's duplex engine cannot be copied across as written. `OnDevice/VoiceDuplexEngine.swift` sets the ducking configuration (`:132-133`), which does not exist on watchOS.

## Conclusion for C13

- The watch could try voice processing. Nothing in the SDK rules it out.
- "Declared" is not "works". The simulator does not model the watch's speaker, microphone and enclosure. Echo cancellation on a wrist speaker a few centimetres from the microphone is a hardware matter.
- Even with working echo cancellation, listening while speaking needs more than the flag: a capture path that stays open during playback, a phone-side pipeline that accepts overlapping audio, and a rule for how a spoken interruption cancels playback. Section 12 of the spec already puts this outside the spec.

The spec's wording ("Phase 0 checks whether echo cancellation works on watchOS") should be narrowed to what phase 0 found: the API is present, and the effect is unmeasured.

## Still unknown, hardware only

- Whether `setVoiceProcessingEnabled(true)` returns success on a real watch, at the watchOS versions the app supports (10 and later, `WATCHOS_DEPLOYMENT_TARGET = 10.0`).
- How much of the 24 kHz speech reaches the 16 kHz capture after cancellation, for example by recording the capture while a known sentence plays, with and without the flag.
- Battery cost and any change to the input format once the mode is on.
- Whether `.playAndRecord` with `.voiceChat` already applies system processing on the watch when the flag is off.
