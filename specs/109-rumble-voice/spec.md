# Spec 109: Rumble voice

**Date:** 2026-11-20. **Status:** draft for owner review. Nothing here is built.
**Companions:** [plan.md](plan.md) (phases and exit criteria), [tasks.md](tasks.md),
[research.md](research.md) (evidence, with file and source references).
**Builds on:** [spec 105](../105-decentralized-personal-mesh/spec.md) section 12 and
[spec 106](../106-sign-in-free-clients/spec.md).

**Scope:** AstralProjection (the Apple clients), AstralDeep (rules, spec 105 amendments, pins) and
possibly AstralKernel (one provider-state question, section 9.3). iPhone, Mac and Apple Watch.

**Out of scope:** the web shell, Android, Windows, and any hosted speech service.

---

## 1. How to read this spec

- A sentence marked **today** describes code that exists now. [research.md](research.md) has the file
  references.
- A sentence marked **proposed** describes this design. Nothing proposed exists.
- O is the owner's decision, taken on 2026-11-20 unless a date says otherwise. C is this design's call,
  with the alternative that lost. The owner has not reviewed the calls yet. Section 16 lists the ones
  most worth a look.

| Word | Meaning |
|---|---|
| Rumble | Rumble 2 Flash, a speech recognition model. It turns speech into text and does nothing else. |
| The pack | The download that holds Rumble, its audio encoder, LFM2.5 and, if one qualifies, the local English voice. |
| LFM2.5 | LiquidAI LFM2.5 1.2B Instruct, the small local chat model inside the pack. |
| Local voice | An English speech model that runs on the device, chosen in phase 0 from the shortlist in section 7.4. The pilot used Piper here. This spec does not. |
| System voice | A voice installed on the device, reached through `AVSpeechSynthesizer`. |
| Provider | The model the owner connected for chat, reached through the kernel. |
| The pilot | The test app at `~/Desktop/AstralVoicePilot`. The lab is `~/Desktop/VoiceTesting`. |
| Overlay | The voice screen that covers the chat during a spoken conversation. |

---

## 2. Summary

**Today** the Apple apps use one model, LFM3-Audio-2B, for hearing, speaking and short
acknowledgements. Voice runs inline in the composer. It speaks English only, in one voice.

**Proposed:** replace that model with the stack the pilot proved on an iPhone 13 Pro, and give voice
its own screen.

1. **Rumble hears.** It transcribes in eight languages.
2. **English is spoken by a local voice model with an open licence,** if one proves as fast as the
   pilot's Piper. If none does, English uses the default system voice. The seven other languages use a
   system voice either way.
3. **The provider answers, through the kernel,** exactly as a typed message would.
4. **LFM2.5 covers for the provider when it goes offline.** Astral says the provider is offline, keeps
   talking with local resources only, and says so again when the provider is back. While the provider
   is online, LFM2.5 only drafts the short spoken acknowledgment (C3).
5. **A waveform overlay** covers the chat while the person talks. It shows no transcript.
6. **The watch relays audio to the phone,** as it does today.
7. **LFM3 and MLX leave the apps.**

The pilot was close, and it felt clunky. The owner named two causes: filler lines that sounded canned,
and interruptions that went wrong. Section 8 is about fixing those. Natural conversation is the main
goal of this spec, ahead of everything else in it.

---

## 3. Goals, non-goals, anti-goals

**Goals**

- Talking to Astral feels like talking to a person. Replies start fast, fillers vary and fit the
  question, and cutting in works the way it does between people.
- The mic icon in a chat starts the whole thing. With no pack installed, the tap leads to the download
  and then straight into the conversation.
- The same conversation works on iPhone and Mac, and on the watch through the phone.
- Eight languages: English, Arabic, Chinese, French, German, Japanese, Korean and Spanish.
- When the provider is down, the person can keep talking. The app says so and recovers by itself.
- Every spoken exchange lands in the kernel log, under the same gates as typed input.

**Non-goals**

- The pilot's world clock tour, its clock tool, its Factory settings screen and its measurement
  harness.
- Voice in the web shell, on Android or on Windows.
- New languages beyond the eight, or downloadable voices for the seven non-English ones.
- Wake words, always-on listening, or voice while the app is closed.
- Voice approval of risky actions.

**Anti-goals.** Any of these makes the feature a failure even if it works.

- A conversation the kernel never sees.
- A system voice or a hosted service standing in without the person being told.
- A canned sentence before every answer.
- An answer that stops for a cough, or keeps going when the person clearly cut in.
- Audio leaving the device for recognition or speech.

---

## 4. Decisions

### 4.1 The owner's decisions

| Id | Decision |
|---|---|
| O1 | Swap the voice model in the Apple apps for Rumble. The seven languages other than English use the default system voice. |
| O2 | A system voice may speak when it is the chosen voice for that language. If the local English voice fails to load, the app switches English to the system voice by itself and says that it did. This changes spec 105 section 12.1. |
| O3 | Remove LFM3 entirely. Use LFM2.5 from the pack as the local model. Normal turns follow the ecosystem's normal conversation flow and go to the provider. |
| O4 | The overlay shows a waveform and no transcript. |
| O5 | The conversation must sound and flow as naturally as possible. The pilot's canned fillers and its interruption handling were the clunky parts. |
| O6 | Live testing: when the build is ready, the owner connects the iPhone. The agent sideloads the app and holds the conversation through the laptop's speakers and microphone. |
| O7 | A mic tap with no pack installed downloads Rumble from the Kentucky Open Science organization on Hugging Face and the voice model from its own page. |
| O8 | Voice works on iPhone and Mac. The watch connects to the phone, or does whatever Apple recommends. |
| O9 | English offers two voices in Settings, the local voice as the default and the system voice, each with a preview. Decided 2026-11-19 for Piper and carried over to its replacement. |
| O10 | When the provider goes offline during a conversation, Astral says "Your LLM model provider is offline." and keeps talking using local resources only. Those exchanges are recorded in the text conversation. When the provider returns, Astral says "Your LLM model provider is back online." and sends queries to it as normal. The owner called this an edge case and asked that it weigh less than the rest. |
| O11 | Piper was only part of the pilot. Use a similar model with a more open licence and similar speed if one exists. Otherwise use the default iOS English voice. |

### 4.2 This design's calls

| Id | Call | Alternative that lost |
|---|---|---|
| C1 | Swap the models underneath the shipping `VoicePipeline`. | Port the pilot's own loop. The shipping pipeline already has the better turn-taking, the integrity checks and the tests. |
| C2 | Every local exchange is written to the kernel log. | The pilot's transcript in the app container. It never syncs and sits outside the gates. |
| C3 | LFM2.5 drafts each acknowledgment and code checks it. | The pilot's regular expressions, and today's six fixed phrases. Both sound canned. |
| C4 | No acknowledgment when the answer is already arriving, or when the turn is small talk. | An acknowledgment before every answer. |
| C5 | An interruption counts only when Rumble hears words in it. | Stopping on sound level alone. That is what made the pilot stall on noise. |
| C6 | Keep the shipping two-stage end of turn, with word lists for all eight languages. | The pilot's fixed 0.3 s of silence. |
| C7 | System voices render to buffers and play through the app's own audio engine. | Letting the system play them directly. Echo cancellation and interruption would not apply. |
| C8 | The local English voice is chosen in phase 0 by measuring three shortlisted models on the iPhone 13 Pro (section 7.4). If none passes, English uses the system voice and no speech model ships. | Naming a winner from published figures. None of the three has a published iPhone number. |
| C9 | Keep the existing download sheet. The overlay opens by itself when the download finishes. | Starting a 1.3 GB transfer with no confirmation. |
| C10 | One new local package, `AstralVoiceRuntime`, wraps llama.cpp as an xcframework built by a pinned script. The voice model, if one qualifies, runs through the FluidAudio Swift package on Core ML. | Sherpa-ONNX, which links eSpeak NG under GPL-3.0. |
| C11 | Delete the installed LFM3 weights on first launch of the new version. | Keeping 1.42 GB that nothing reads. |
| C12 | In a voice session the local talker is always LFM2.5. Typed chat keeps today's order, Foundation Models first, with LFM2.5 where LFM3 was. | Foundation Models in voice too. Its speed and languages vary by device. |
| C13 | The watch keeps the WatchConnectivity relay and stays tap-to-interrupt. | Direct networking from the watch. Spec 106 removed the watch's network identity. |
| C14 | The overlay covers the chat on iPhone, the chat pane on Mac and the whole screen on the watch. | A floating panel on Mac. |
| C15 | Timing budgets are targets measured in the live pass. CI does not gate on them. | Wall-clock gates in CI, which the engineering rules forbid. |
| C16 | The live listening pass is required on iPhone only. Mac gets a live check by the agent. The watch qualifies by code tests. | Requiring the owner's ears on all three. |
| C17 | While the provider is online, the local model never answers a question. Something said during a running job becomes the next provider turn, and saying stop cancels the job. This follows from O10. | The pilot's side chat with the local model during a job. |

---

## 5. What the person experiences

### 5.1 The first tap

1. The person taps the mic in a chat.
2. No pack is installed, so a sheet appears. It names the download and its size, about 1.3 GB, before it
   asks for anything else.
3. The person taps Download. On cellular or a constrained network the app asks once more.
4. Progress shows in the sheet. The person can leave it and keep typing. The download resumes after a
   relaunch.
5. When the last file verifies, the sheet closes and the overlay opens, provided the person is still in
   that chat and the app is in front. Otherwise the mic shows that voice is ready.
6. The app asks for the microphone the first time the overlay opens.

### 5.2 A conversation

```
+------------------------------+
| [EN v]                   [X] |
|                              |
|                              |
|    ..|.||l|||l||.|..         |
|                              |
|          Listening           |
|                              |
|           [ mute ]           |
+------------------------------+
```

The person talks. The waveform follows their voice.

They stop. Within about half a second Astral starts to answer. For a quick question that is the answer
itself. For one that needs the provider to think or use a tool, it is a short line that fits the
question, followed by the answer when it arrives.

The person can cut in at any point. Astral lowers its voice the moment it hears them, and stops once it
is clear they are saying something. A cough or an "mm-hm" does not stop it.

Closing the overlay ends the session. The chat underneath now holds the whole exchange as text.

### 5.3 When the provider is down

This is an edge case. It gets a simple, predictable design and no more (O10).

The person is talking normally and the provider stops answering. Astral says:

> Your LLM model provider is offline.

Then it keeps talking, using the local model only. If a question was waiting on the provider when it
failed, the local model answers that question, so the person does not have to repeat it. The state
word under the waveform reads Offline.

Local answers are shorter and less capable. No tool runs and nothing leaves the device.

Every offline exchange is written into the chat as text, like any other turn, and marked as answered
on this device.

The app keeps checking quietly. When the provider answers again, Astral waits for a pause and says:

> Your LLM model provider is back online.

From the next thing the person says, queries go to the provider as normal. The provider sees the
offline exchanges as part of the chat, so the conversation carries on where it was.

Both sentences are spoken in the session's language.

### 5.4 Languages

The menu at the top left lists the eight languages. Picking one sets three things together: the
language Rumble listens for, the language Astral replies in, and the voice.

English uses the local voice unless the owner chose the system voice in Settings, or no local voice
qualified. The other seven use the
default installed system voice for that language. If the device has no voice for it, the app says how
to install one and does not start.

The choice is saved on that device. The first default is the device's language when it is one of the
eight, and English otherwise.

### 5.5 Mac

The same mic, the same sheet and the same overlay, covering the chat pane. The sidebar stays visible.
Escape closes the overlay. Intel Macs cannot run the pack and say so, as they do today.

### 5.6 Watch

The person taps the mic on the watch. The watch asks the paired iPhone to start, shows the waveform,
and streams the microphone to the phone. The phone does all the model work and streams speech back.

The phone must be reachable, with the pack installed and its mesh open. When it is not, the watch says
which of those is missing.

The watch listens and speaks in turns. Tapping the screen interrupts.

---

## 6. The pack and its download

**Proposed:** the app bundles its own manifest in place of today's LFM3 manifest. It lists every file,
with its repository, pinned revision, byte count and SHA-256.

| Part | Source on Hugging Face | Bytes |
|---|---|---:|
| Rumble recognition, GGUF | `Kentucky-Open-Science/Rumble-2-Flash-Astral`, pinned revision | 283,424,640 |
| Rumble audio encoder, GGUF | Same | 214,393,024 |
| LFM2.5 1.2B Instruct, GGUF | Same | 730,895,168 |
| Local English voice, Core ML | The chosen model's repository, pinned revision | 34 to 171 MB for the small candidates. Set after phase 0. |

The three GGUF files total 1,228,712,832 bytes. With no local voice the download is that and nothing
more.

The published v1.1 manifest in the Kentucky Open Science repository lists a Piper archive. The app does
not use that manifest.

The existing store keeps its behavior: one file at a time with resume, a byte count and SHA-256 for
each, staging, an atomic swap, an install marker bound to the whole manifest, a free-space check, the
cellular confirmation, and exclusion from backup. Digests are checked again before any model loads.

Two things are new.

- **Two repositories.** The manifest names both. The app contacts no other repository for the pack. The
  voice model loads from the verified local folder, and the speech library is never allowed to fetch
  files by itself.
- **A partial outcome.** If the three GGUF files install and the voice files keep failing, the sheet
  offers one extra button: continue with the system voice for English. The app retries the voice later
  on its own and tells the person when it arrives.

The mirror setting stays, with its HTTPS rule.

Consent given for the LFM3 download does not carry over. The person confirms the new pack once.

---

## 7. Runtime

### 7.1 The seam

**Today** `VoicePipeline` talks to its model through `VoiceModelInference`, which has three functions:
`transcribe`, `synthesize` and `acknowledgement`.

**Proposed:** keep the pipeline and split the seam by job.

| Job | Implementation | Notes |
|---|---|---|
| Recognition | Rumble through llama.cpp with `libmtmd` | 16 kHz mono. Takes a language. Partials by re-running on the growing segment, as today. |
| English speech | The local voice through Core ML, or the system voice | One sentence at a time. A streaming model starts playing before the sentence is finished. |
| Other speech | `AVSpeechSynthesizer.write` | Rendered to buffers, never played by the system directly (C7). |
| Local text | LFM2.5 through llama.cpp | Acknowledgments, local talk and the kernel's on-device model. |

All speech enters the existing duplex engine after one resampling step. Echo cancellation, ducking,
interruption and the rule that only played speech is recorded then apply to every voice alike.

Rumble and LFM2.5 share one llama.cpp runtime and never run on the GPU at the same instant. Recognition
has priority, because a late transcript delays everything after it.

### 7.2 The package

A new local Swift package, `AstralVoiceRuntime`, replaces `AstralVoiceModel`.

- It wraps llama.cpp at a pinned revision, built with `libmtmd`. Nothing else.
- A script in the repository builds the xcframeworks, the way the kernel's Apple build script does.
  Git ignores the outputs.
- Slices: iPhone, iPhone simulator and Mac, all arm64. Metal on devices and Macs, CPU in the
  simulator.
- The watch target does not link it.

The simulator gets a CPU build so tests can run real inference on tiny fixture models. The device gate
still keeps the feature off in the simulator for users.

### 7.3 Loading

Models load when the overlay opens. The waveform shows a "Getting ready" state until the microphone is
live. On the iPhone 13 Pro that took about 3.8 s in the pilot.

The voice warms in parallel with the microphone. Models unload 60 seconds after the overlay closes, or
at once under memory pressure.

The device gate stays as it is until phase 0 measures it: Apple silicon, and at least 5.5 GB of memory
on a phone.

### 7.4 Choosing the English voice

The owner's rule (O11): a model like Piper with a more open licence and similar speed, or else the
system voice.

Piper itself is out. Its runtime links eSpeak NG, which is GPL-3.0, and the rights to redistribute its
Lessac voice are unclear.

Three models pass the licence test on paper. All three run on Core ML through FluidAudio, an Apache-2.0
Swift package with no GPL parts, so one integration can try all of them.

| Candidate | Licence | Size | Published speed | Concern |
|---|---|---|---|---|
| Kokoro-82M | Apache 2.0 | 82M parameters | 3 to 11 times real time on Apple silicon. About 50 ms per utterance on an M5 Pro. | Whole sentence before audio. One Mac benchmark peaked at 1.5 GB of process memory. No iPhone figure. |
| Phonon 31m | Apache 2.0 | About 171 MB | 19 to 25 times real time on an M5 Pro, about 20 ms to first audio. Streams. | New. English only. |
| Pocket TTS small, 18M | CC-BY-4.0, attribution required | 34 to 145 MB | 28 times real time on an M5 Pro. Streams in 80 ms frames. | Released October 2026. Kyutai's own repository asks the downloader to accept terms. |

The newer Core ML builds need iOS 18 or macOS 15. The apps still support iOS 17 and macOS 14. On those,
English uses the system voice.

**The test, in phase 0.** Each candidate speaks the pilot's ten acknowledgment lines on the iPhone 13
Pro, with Rumble and LFM2.5 loaded. A candidate passes when all of these hold:

- No GPL code and no field-of-use restriction, confirmed from the licence files.
- Median time to first word within 0.25 s of Piper's measured 0.54 s, so the 0.8 s target in section
  8.5 still holds.
- The app stays inside its memory budget with all three models loaded.
- The model loads from a local folder with no network request.

The owner then listens to the ones that pass and picks. If none passes, or the owner likes none of
them, English uses the system voice, FluidAudio is not added, and the Settings control goes away.

**Considered and rejected.**

- **Supertonic 3.** Fast, with 31 languages. Its OpenRAIL-M licence forbids use "to provide medical
  advice and medical results interpretation". Astral is a clinical platform. The project was also
  archived on 2026-09-09 with no further support.
- **KittenTTS 2.** The small models are Apache 2.0. There is no iPhone runtime, and the one portable
  path uses eSpeak NG data.
- **Matcha.** Its English model needs eSpeak NG.
- **Pocket TTS 100M.** About 1.8 times real time on a Mac. Too slow.

---

## 8. Making it sound natural

This is the heart of the spec. Four things decide how a spoken conversation feels: when Astral decides
the person is done, what it says first, how it handles being cut off, and how its speech is paced.

### 8.1 Knowing when the person is done

**Today** the pipeline speculates after 0.25 s of silence and ends the turn after 0.6 s. If the last
word was a continuation word such as "and" or "because", it waits 0.4 s more. The word list is English.

**Proposed:** keep that design (C6).

- Rumble produces a partial transcript about twice a second while the person talks.
- Each of the eight languages gets its own continuation list. A language with no list uses the plain
  0.6 s.
- A partial that ends mid-number or mid-list also earns the extension.
- The live pass tunes the three durations. The values above are the starting point.

### 8.2 What Astral says first

The pilot opened nearly every answer with a sentence built from a pattern, such as "Let me look into
the colors of apples." Ten of those in a row sound like a machine. The shipping app picks from six
fixed phrases, which is worse.

**Proposed:** three rules.

**Rule 1. Say nothing extra when the answer is quick.** The acknowledgment is held for a short window
after the end of the turn. If the first sentence of the real answer is ready inside that window, Astral
speaks the answer and the acknowledgment is thrown away. The window starts at 0.3 s and is tuned in the
live pass.

The hold costs no time when the filler is used. The filler is drafted and synthesized before and
during the window, so its audio is ready to play the instant the window closes.

**Rule 2. Small talk gets an answer, with no filler.** Greetings, thanks, yes, no and one-word replies
never get an acknowledgment.

**Rule 3. When a filler is needed, the model writes it and code checks it.**

LFM2.5 starts drafting at the speculation point, from the partial transcript, so the line is usually
ready when the turn ends. The voice synthesizes an accepted draft at once. It sees the question, the
language and the last three openers it used.

The draft runs in the gap between two Rumble partials. If the final transcript differs from the
partial the draft was written for, the draft is checked against the final text and dropped when it no
longer fits.

Code accepts the draft only if all of these hold:

- At most twelve words and one sentence.
- In the session's language.
- No question mark.
- No digits and no claim that something was done, found or sent.
- Its first two words differ from the last three openers.
- The request did not look private or technical. Phone numbers, addresses, keys and the like get a
  generic line.

A rejected draft falls back to a short generic line, rotated so the same one never plays twice in a
row. The model proposes and code decides.

If the person keeps talking after the speculation point, the draft is discarded.

**Longer waits.** A tool call or a slow answer gets one spoken progress line, drafted the same way from
the kernel's own progress events. At most one such line every 15 seconds.

### 8.3 Being interrupted

The pilot stopped its answer on any burst of sound, then sometimes waited on a segment with no words in
it. The owner felt that.

**Proposed:** an interruption has three stages (C5).

1. **Sound over speech.** The moment the detector hears the person while Astral is talking, playback
   drops to 15 percent. Nothing is cancelled. This is today's behavior.
2. **Is it speech from this person?** The detector's threshold rises with the recent level of the
   person's own voice, so quieter room sound does not count. This comes from the pilot.
3. **Does it have words?** Rumble transcribes the overlapping audio. The interruption is confirmed
   only when the transcript has words that are not a backchannel.

Stage 3 trusts Rumble to return nothing for noise. Rumble can add unsaid words to very short clips, and
the pilot saw stray quiet transcriptions displace replies. So a clip shorter than 0.4 s never confirms
an interruption by itself, and phase 0 measures how often Rumble invents words on coughs and room
noise (R11). If that rate is too high, stage 3 also requires the speech to last past the confirmation
delay.

Then one of three things happens.

| What was heard | What Astral does |
|---|---|
| Nothing, or noise | Volume returns. The answer continues from where it was. |
| A backchannel: "mm-hm", "okay", "right" | Volume returns. The answer continues. Each language has its own list. |
| Real words | Speech stops. The unplayed part of the answer is dropped. The new words become the next turn. |

"Stop", "wait" and "hold on", in each language, stop speech at once and also cancel a running job when
that job is safe to cancel. A pending risky action is never cancelled or approved by voice.

Only the sentences that finished playing are recorded as spoken. That rule is unchanged.

After Astral finishes speaking, capture stays as it is. Echo cancellation in the audio engine keeps
Astral from hearing itself.

### 8.4 How the speech is paced

- The provider's answer streams sentence by sentence into the voice. Speech starts on the first
  sentence and never waits for the whole answer.
- Each sentence is fully synthesized before it plays, so there are no gaps inside a sentence. The next
  one is synthesized while the current one plays.
- Voice turns carry a preference to the provider for short, conversational answers, with more detail
  when the person asks for it. The pilot did this.
- Markup, links, code and table syntax are stripped before speech. A result that is a component speaks
  its `spoken` form.
- If an answer arrives while a filler is playing, the filler finishes and the answer follows with no
  pause.

### 8.5 Targets

These are targets for the live pass on an iPhone 13 Pro, warm, with the local English voice. CI does not gate on them
(C15).

| Moment | Target |
|---|---|
| End of the person's turn to the first audible word | Median 0.8 s or less. The pilot measured 0.54 s with Piper and 1.09 s with the system voice, both with no hold. This design adds a 0.3 s hold. |
| The person starts talking over Astral, to lowered volume | 150 ms or less |
| A confirmed interruption, to silence | 600 ms or less from the first word |
| Mic tap to the overlay on screen | 300 ms or less |
| Overlay open to listening, cold | 4 s or less |

---

## 9. The kernel, safety and the log

### 9.1 What does not change

- A recognized utterance becomes an `OwnerVoiceTurn`. It has owner integrity only when it came from
  this device's microphone, or a paired watch's, in a session the owner started.
- The text goes to the kernel as an ordinary message. Tools, permissions, PHI gates, consent and audit
  apply as they do to typed input.
- Voice cannot approve anything. When a consent prompt appears, Astral says that something needs
  approval, and the normal consent sheet opens over the overlay.
- Audio from a file, a call or a video is untrusted.
- Only speech that played is written to `chat.spoken`.

### 9.2 The local model's two jobs

| Job | When | Reaches the kernel as |
|---|---|---|
| The acknowledgment and progress lines | Provider online | `chat.spoken` records, as today |
| The whole answer | Provider offline | A kernel turn served by the on-device model, in the same chat (C2) |

While the provider is online the local model answers nothing (C17). Something the person says during a
running job is queued as the next provider turn. "Stop" cancels the job when it is safe to cancel.

LFM2.5 replaces LFM3 as the host's `OnDeviceModel`. The existing limits stay: plain text only, bounded
input and output, no structured schemas.

PHI rules already treat the device as a named destination. A local answer follows them.

### 9.3 The one kernel question

The app needs two things from the kernel: a clear signal that the provider is unreachable, and a way to
serve a turn from the on-device model in the same chat while it is.

Phase 0 must establish whether both exist today. If they do, nothing changes in the kernel. If one is
missing, the smallest change that adds it is specified and reviewed before phase 3 starts.

### 9.4 Provider failure and return

**Going offline.** The switch happens on a kernel signal that the provider is unreachable, timed out or
refused the request. A slow answer does not trigger it. Astral speaks the offline sentence once, then
answers the waiting question locally.

**While offline.** Each turn is a kernel turn served by the on-device model. It lands in the chat as
text, marked as answered on this device. No request carrying the person's words leaves the device.

**Checking.** The app checks whether the provider is back before each turn and on a timer between
turns, starting at 15 seconds and backing off to one minute. The check carries no conversation
content.

**Coming back.** When a check passes, Astral speaks the back-online sentence at the next pause. It
never talks over the person and never cuts off its own sentence to say it. The next turn goes to the
provider with the full chat, offline exchanges included.

If the provider fails again, the same steps repeat. Each change of state is announced once.

In typed chat, with no voice session open, nothing is spoken. The same switch applies and the chat
shows the two notices as text.

---

## 10. The overlay

One view for iPhone and Mac, and a smaller one for the watch.

| Element | Behavior |
|---|---|
| Waveform | Follows the microphone while listening and the output while speaking. A slow, calm motion while thinking. Flat while muted. |
| State word | One of: Getting ready, Listening, Thinking, Speaking, Offline, Muted. |
| Language menu | Top left. Changing it starts a fresh capture. |
| Close | Top right. Ends the session and returns to the chat. |
| Mute | Bottom. Stops capture and leaves speech playing. |

There are no captions and no transcript (O4). The chat underneath receives every turn as text, and it
is there when the overlay closes.

Errors appear as one line of text in place of the state word, with a button when there is something to
do: grant the microphone, install a voice, retry the download.

**Accessibility.**

- VoiceOver reads state changes. Every control has a name and keeps a stable identifier for tests.
- With Reduce Motion on, the waveform becomes level bars that step and do not flow.
- State is never shown by color alone.
- A person who cannot hear the conversation still has the full text in the chat and typed input beside
  it.

The session ends when the app leaves the foreground, the device locks, or the person moves to another
chat. Coming back does not restart the microphone. This matches today.

The composer's inline "Listening" state goes away. The mic opens the overlay.

---

## 11. Settings

The Voice row in Settings keeps its job and gains two controls.

- **English voice.** The local voice, the default, or the system voice. Each has a preview button that plays one
  fixed sentence, whatever the saved choice is (O9).
- **Language.** The same list as the overlay menu.

It also shows the pack's status, its size, and Remove.

Both choices are saved on the device. They do not sync.

If the local voice fails to load, the app switches English to the system voice, shows a notice in the
overlay and in Settings, and keeps the notice until the person dismisses it or the voice loads again
(O2).

If no local voice qualified in phase 0, the English voice control is absent and English uses the
system voice.

---

## 12. The watch

**Today** the watch captures 16 kHz audio, sends it to the phone over WatchConnectivity, and plays the
24 kHz speech the phone sends back. It has no model and no network of its own.

**Proposed:** keep all of it (C13). The packet format does not change.

What changes on the phone side: the relay builds its pipeline on the new runtime, and local-voice or
system-voice output is resampled to 24 kHz before it is packed.

What changes on the watch: the mic opens a waveform screen in place of today's button and label. The
language follows the phone's saved choice.

This is also what Apple's rules point to. A watch app may open its own network connection only while
it streams audio or runs a call, and spec 106 took the watch's network identity away on purpose.
WatchConnectivity is the route Apple provides for immediate exchange between a watch app and its phone
app.

Two limits are stated plainly to the person and tested in code.

- The phone must be reachable. Losing it ends the session with a spoken and visible notice.
- The watch does not listen while it speaks. A tap interrupts.

Phase 0 checks whether echo cancellation works on watchOS. If it does, spoken interruption on the
watch becomes a follow-up, outside this spec.

---

## 13. Retiring LFM3

- The `AstralVoiceModel` package, the MLX dependency and the LFM3 manifest are deleted.
- On first launch after the update, the app deletes the installed LFM3 weights and their staging folder
  (C11). That frees 1.42 GB.
- `VerifiedLFM3OnDeviceModel` is replaced by an LFM2.5 model with the same limits.
- The unused `NSSpeechRecognitionUsageDescription` leaves `Info.plist`.
- Tests that name LFM3 or MLX are rewritten against the new runtime or removed with the code they
  covered.

---

## 14. Rules and documents that change

| Where | Change |
|---|---|
| `AGENTS.md`, Mental model | The voice sentence allows a system voice as a chosen output, with a visible notice when it replaces the local voice. Hosted speech stays forbidden. |
| Spec 105 section 12.1 | A dated amendment: the platform table names Rumble, LFM2.5, the local English voice and the system voices. |
| Spec 105 section 12.5 | A dated amendment: spec 109 adds a required live pass on iPhone. Spec 105's own qualification is untouched. |
| `apple-clients/ONDEVICE-VOICE.md` | Rewritten for the new stack. |
| `apple-clients/README.md` | The voice lines. |
| `tests/test_apple_no_hosted_voice.py` | `AVSpeechSynthesizer` is allowed in one named file. Every other pattern stays forbidden everywhere. |
| App acknowledgments | Rumble, LFM2.5, llama.cpp, and the chosen voice model with its speech library. A CC-BY voice gets its attribution line. |
| `config/astral-composition.json` | The Projection pin, when the work merges. |
| Vault | `synthesis-astral-conversational-voice-interface` and `project-astral`, at each checkpoint. |

`ui_protocol.json` does not change. Voice is local to each Apple client, so no other client needs a
matching change.

---

## 15. Qualification

**Code tests** cover every requirement that code can check, with fixture audio, fixture models, a fake
clock and a fake provider, as spec 105 section 12.5 describes. They include:

- The store: both repositories, path escapes, tampering, the partial outcome, and that the speech
  library makes no network request.
- The acknowledgment checker: every rejection rule, opener rotation, the quick-answer window.
- Interruption: noise, a backchannel and real words, each in at least English and one other language.
- End of turn with continuation words in all eight languages.
- Provider failure: both announcements, each spoken once, local turns written to the chat as text, the
  waiting question answered locally, recovery, and that no request carries the person's words while
  offline.
- Speech during a running job with the provider online becomes the next provider turn.
- Local exchanges reaching the kernel log.
- Integrity: a file or programmatic source never produces an owner turn. Saying yes approves nothing.
- The system voice path: buffers only, the automatic switch from the local voice, and its notice.
- The watch relay on the new runtime.
- The LFM3 cleanup.

**The live pass on iPhone is required (O6).** When the build is ready the agent asks the owner to
connect the phone. The agent installs a localtest build under the rules in `AGENTS.md`, plays scripted
speech through the laptop's speakers, records the replies through its microphone, and reports.

| Part | Content |
|---|---|
| Clean install | Mic tap, download, overlay, first turn |
| Ten varied questions | With the local voice, then with the system voice |
| Interruption | Real words, a cough, a backchannel, and a stop command |
| Offline | Provider made unreachable mid-conversation, the offline sentence, three local turns that appear in the chat, provider restored, the back-online sentence, one provider turn |
| A second language | Spanish, four turns |
| Timings | Every target in section 8.5 |

The owner then talks to it and decides whether it feels natural. That judgment is the gate. The
recordings and the timing report are kept with the spec's evidence.

**Mac** gets the same script run by the agent on this Mac, without a required sign-off (C16).

**Watch** qualifies by code tests. A live watch check is welcome and blocks nothing.

---

## 16. Calls worth the owner's eye

Most calls in section 4.2 follow from the owner's answers. Four do more than that.

- **C17.** With the provider online, the local model stays out of the conversation. The pilot let it
  chat during a running job. This design drops that, reading O10 to mean local talk is for the offline
  case only.
- **C4.** Astral often says no filler at all. The pilot always said one.
- **C8.** The English voice is not picked today. Three models have clean licences, and none has a
  published iPhone figure, so phase 0 measures them on the iPhone 13 Pro and the owner listens.
- **C16.** Only the iPhone needs the owner's ears.

---

## 17. Open questions

| # | Question | Who answers | Needed by |
|---|---|---|---|
| 1 | LFM2.5 ships under the LFM Open License v1.0. Commercial use is licensed only while the legal entity, counted with every entity it controls or is controlled by, has annual revenue under $10 million. A qualified non-profit is exempt for non-commercial and research use. Which entity distributes Astral, and does it fit? | The owner | End of phase 0. If the answer is no, the local model needs a commercial licence from Liquid AI or a replacement. |
| 2 | Which shortlisted voice sounds right? | The owner, by ear, from phase 0 recordings | End of phase 0 |

---

## 18. Approaches considered

**A. Port the pilot.** Bring `Pilot.swift` and its loop into the app. Fastest to a demo, and it carries
over the exact behavior the owner found clunky. It also bypasses the integrity checks and the watch
relay the shipping pipeline has. Rejected.

**B. Swap the models under the shipping pipeline and add the overlay.** Keep turn-taking, interruption,
the kernel link and the watch relay. Replace the model seam, the store's manifest and the composer's
inline state. Add the pilot's good parts: the language triple, the speaker-level threshold, the English
voice choice. This is the design (C1).

**C. One audio model for everything.** Stay with a single model that hears and speaks, as LFM3 does. The
lab measured LFM3 at a 2.76 s median to first audio against 1.57 s for Rumble with LFM2.5, in English
only. Rejected by O1.
