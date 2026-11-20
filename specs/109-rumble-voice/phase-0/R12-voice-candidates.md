# R12. Voice candidates on the iPhone 13 Pro

## Question

How fast is each English voice candidate to first audio with Rumble and LFM2.5 1.2B loaded, and what memory does it add?

## Answer

- **Phonon 31m and Pocket TTS 18m pass the speed test. Kokoro passes it only with leading silence removed. Piper is the reference.** All numbers below are on the bench clock, not the pilot clock.
- Median to first audible sample, bench clock, pass 1 of the ten lines: Pocket 18m **0.048 s** (but see the onset note), Phonon **0.169 s**, Piper **0.210 s**, Kokoro **0.522 s**.
- Memory: no candidate raised `phys_footprint` more than about 210 MB above the pre-voice reading. Kokoro added the most (699 MB peak against 491 MB before). Pocket and Phonon added under 50 MB.
- The tests in spec section 7.4 are marked per candidate below. **Kokoro is marked pending licence.** The owner decides by listening to `phone/recordings/<candidate>/`.

## The clock

**measured.** The pilot's 0.539 s for Piper ran from the detector declaring the turn over to the first non-silent output-mixer sample (above 0.003) plus the route's reported latency, in a Release build (`AstralVoicePilot/INTEGRATION.md`, "Fresh-chat English voice comparison", and `Integration/Sound.swift:61-67`). That figure includes Rumble recognition of the question, building the line, synthesis and the mixer. The pilot's JSON has no split between them (`rounds[]` keys list only `firstWordMilliseconds` and later events).

**This bench's clock** starts at the `synthesize` call for text that is already known. It stops at the first non-silent sample (above 0.003, the pilot's threshold) of the produced audio. For a non-streaming voice that is the return time plus the offset of the first non-silent sample. For a streaming voice it is the arrival of the frame holding that sample plus its offset in the frame, never earlier than continuous playout from the first frame. The output route latency and the mixer are not included, so **no bench number is comparable to 0.539 s**. The comparable quantity is the difference to Piper on this same clock. Piper measured here is **0.210 s**, so about 0.33 s of the pilot's 0.539 s is recognition, line building and the audio route.

Route latency: not measured. The bench plays no audio. The pilot's own `outputNode.presentationLatency` is the figure to use.

## Setup

- Device: iPhone 13 Pro (iPhone14,2), iOS 27.2.1, 5,941,428,224 bytes of physical memory, thermal state nominal at start and end of every run, low power mode off. **measured**
- Build: Release, bundle id `org.kentuckyopenscience.edgevoicelab` (an in-place upgrade of the lab app, signed with the lab's own wildcard profile). The frozen lab IPA was reinstalled afterwards. The 415 model files were intact.
- Runtime: the lab's `libVoiceInference.a` and `Inference.cpp` (llama.cpp `8af68d486a9473820d24d00af8f33334ca80124d`, Metal, `libmtmd`), copied from `VoiceTesting`. Rumble and the audio encoder, and LFM2.5 1.2B, loaded on the GPU and warmed with one generation each before the "before voice" reading, so they are resident.
- FluidAudio `v0.18.4`, commit `e0c6103bc87a88d25276cad491c86e757f58c865`. The spec names 0.17.7, which lacks Phonon and Pocket small (see R5a). Every number here is from 0.18.4. Kokoro was not measured on 0.17.7.
- Kokoro: legacy 7-stage runtime (`KokoroAneManager`), voice `af_heart`. Phonon: `phonon-31m-en`, voice `default`. Pocket: `english_18m_2026-10`, voice `alba` (CC BY 4.0). Seeds fixed at 1000 plus the line index.
- Lines: the ten acknowledgments of the pilot's Piper run, `AstralVoicePilot/Results/ack-piper-20261119-141636.json`, `rounds[].acknowledgment`, verbatim including "pendants". They have 6 to 9 words.
- Piper: the lab's Sherpa-ONNX path (`vl_voice_open`, `vl_speak`), files already on the phone, 22,050 Hz.
- Three passes of ten lines per launch, one launch per candidate, 1 s between lines. Recordings come from pass 1 and are at the native rate (24,000 Hz, Piper 22,050 Hz), 16-bit WAV.
- Hashes (SHA-256): Rumble `Rumble-2-Flash-AD-Q5_K_M.gguf` `c49430ce4eb24ca6e6ff154f135c63a0dd98fc2247746824f40d96df7d2bd7ee`. Encoder `mmproj-Rumble-2-Flash-Q8_0.gguf` `97116d3656cbe4912ac9aee43eb4a887832121e2a2c07c5ee61617052bdd30f8`. LFM2.5 `LFM2.5-1.2B-Instruct-Q4_K_M.gguf` `b1b3de114215d9507409a662a501a631095a479a419584e8a2ded6304b19b4f5`. Piper `en_US-lessac-medium.onnx` `4ba07d8549906668ee855fd9abf9faf66c5db74742712ff026a159f7277fca9f`. Voice packs: Hugging Face `FluidInference/pocket-tts-coreml` at `c2e998e269e4d4b51f74061faddb8450cd8f9a9a`, `FluidInference/kokoro-82m-coreml` at `51c072023b254cd02368102ab88bcfbff6cad4cd`. Per-file SHA-256 and HTTP status for all 218 files are in `phone/voices/fetch-log-pocket.json` and `fetch-log-kokoro.json`.

## Timing, pass 1 (ten lines), bench clock

| Candidate | Median first audible | Min to max | First line | Median total synthesis | Median audio | Real-time factor |
|---|---:|---:|---:|---:|---:|---:|
| Piper (reference) | 0.210 s | 0.159 to 0.306 | 0.306 s | 0.167 s | 2.26 s | 13.8 |
| Pocket TTS 18m | 0.048 s | 0.030 to 0.054 | 0.030 s | 0.289 s | 2.44 s | 8.4 |
| Phonon 31m | 0.169 s | 0.157 to 0.189 | 0.161 s | 0.394 s | 2.88 s | 7.5 |
| Kokoro-82M (legacy) | 0.522 s | 0.489 to 1.421 | 1.421 s | 0.205 s | 2.83 s | 14.0 |

Medians over all 30 lines match pass 1 within 0.01 s. For Pocket and Phonon, first chunk arrives at 0.050 s. Kokoro and Piper return the whole utterance at once.

**Onset note.** Pocket 18m's first frame carries a faint blip above 0.003 (peak about 0.007) within the first 10 ms, then near silence until speech starts about 0.16 s in. At a speech threshold of 0.02 the median onset is 0.158 s, which puts Pocket's first real speech near **0.21 s**, equal to Piper's. Phonon's lead-in is 0.12 s and is real silence. Kokoro leads with 0.32 s of silence. Its synthesis is 0.205 s, so trimming the silence would put it near 0.21 s. Use the 0.003 threshold only if the pilot's rule is applied literally. Both readings are given for that reason.

Cold and warm: a first launch (cold1) and a later one (warm-nolux) differ by under 0.01 s in medians. Voice load time was 5.3 to 5.9 s for Pocket, 5.3 to 5.7 s for Phonon, 10.2 to 11.0 s for Kokoro, 0.5 s for Piper. The first launch did not show the 10 to 150 s ANE compile the FluidAudio docs mention, so a longer first-ever compile is **unknown** (the system may have compiled earlier or cached). The warm-nolux runs also had the LuxTTS resource bundle removed (see R5a). Rumble loaded in 0.2 to 0.9 s after the first run and 12.9 s on the very first launch after install.

## Memory, bytes in MB

Primary metric is `phys_footprint` (task_vm_info), which the system's limit is judged on. `ru_maxrss` (getrusage, process high-water, cumulative) and `os_proc_available_memory` are listed too. The pilot's 1.85 GB (Piper) and 1.50 GB (iOS voice) were `ru_maxrss`, not footprint, so compare only with the `ru_maxrss` column.

| Candidate | footprint before voice | footprint after load | footprint peak over ten lines | `ru_maxrss` at end | lowest available memory |
|---|---:|---:|---:|---:|---:|
| Piper | 479 | 551 | 658 | 1,640 | 2,563 |
| Pocket 18m | 490 | 470 to 495 | 499 to 538 | 1,616 to 1,643 | 2,683 to 2,722 |
| Phonon 31m | 491 | 490 to 496 | 523 to 528 | 1,572 to 1,636 | 2,694 to 2,698 |
| Kokoro | 491 | 532 to 533 | 698 to 699 | 1,923 to 1,929 | 2,523 |

The footprints are small because the GGUF weights are memory mapped. Core ML voices run partly in a system process whose memory is not in the app's footprint. Footprint peaks include 20 ms sampling only. Piper's 1.85 GB in the pilot was a different metric and a whole app, so the two figures are not a regression. `ru_maxrss` here includes the 1.5 GB of mapped Rumble and LFM2.5 pages.

## Tests in spec section 7.4

| Test | Piper | Pocket 18m | Phonon 31m | Kokoro-82M |
|---|---|---|---|---|
| No GPL, no field-of-use limit | see R9 | see R9 | see R9 | see R9. **Pending licence**: front end data has no stated origin |
| Median first word within 0.25 s of Piper | reference | pass (0.048 s, or about 0.21 s at the 0.02 threshold, against 0.210 s) | pass (0.169 s, 0.04 s faster) | fail as measured (0.522 s is 0.31 s slower), pass if leading silence is trimmed |
| Inside the memory budget with all three models | no budget number exists in the spec, so no pass or fail. Footprints above are all under 700 MB | same | same | same |
| Loads from a local folder with no network request | not applicable | pass (R5a) | pass (R5a) | pass with a pre-seeded layout (R5a) |

The 0.25 s test was written against the pilot clock. Applied to this clock, "within 0.25 s of Piper" means at most 0.46 s here.

## What is still unknown

- Output route latency and the full turn clock. These need the pilot's pipeline.
- Intelligibility and quality. No automatic check was run. Recordings are in `phone/recordings/` (`line-01.wav` to `line-10.wav` per candidate, plus `deess/` for the Pocket and Phonon files that FluidAudio's own `synthesize` produces with its de-esser).
- A first-ever Core ML compile on a fresh install, and behavior after a long run (upstream reports Kokoro crashes on iOS 27 after about an hour).
- Kokoro `.v3`, Pocket 7m and 2m, and other voices. Not measured.
- Whether `physicalMemory` of 5.94 GB and 2.5 to 2.7 GB available leave room for the real turn with a pipeline, GPU use by Rumble at the same time, and the app UI.
