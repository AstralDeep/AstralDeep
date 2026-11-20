# R1. Rumble partials every 0.5 s

## Question

Does re-running Rumble on a growing segment every 0.5 s keep up on an iPhone 13 Pro, and does an LFM2.5 draft of at most twelve words plus one synthesized sentence fit in the gap between two partials?

## Answer

- **A 2 s segment keeps up, a 5 s segment barely does, a 10 s segment does not.** One partial over the whole segment took 0.253 s at 2 s, 0.454 s at 5 s and 0.800 s at 10 s. With a 0.5 s tick, the 10 s partial takes 1.6 ticks.
- **The draft and the sentence do not fit in the gap between two ticks at any length.** The gap is 0.25 s at 2 s, 0.05 s at 5 s and none at 10 s. The draft alone took 0.26 to 0.42 s. One sentence from the fastest candidate (Pocket 18m) took 0.24 to 0.28 s in total, with first audio at 0.05 s.
- Run serially in one process, partial plus draft plus sentence took **0.73 s at 2 s, 1.12 s at 5 s, 1.50 s at 10 s**.
- The design can still work, because speculation starts after 0.25 s of silence, when no new audio is arriving and partials stop being the bottleneck. That is reasoning, not a measurement. It means the draft and sentence run in the silence window and not between ticks, and a continuing speaker cancels them.

## Method

**measured.** iPhone 13 Pro (iPhone14,2), iOS 27.2.1, Release build, the bench app described in R12. Rumble Q5_K_M with its Q8 encoder and LFM2.5 1.2B Q4_K_M loaded through llama.cpp `8af68d486a9473820d24d00af8f33334ca80124d` on Metal, both warmed. Pocket 18m (`alba`) resident and offline. Thermal state nominal at the start and end. Raw data: `phone/results/pull/r1-pocket18-r1a-*/result.json`.

Input: the lab's prerecorded fixtures (`arithmetic`, `correct`, `remember`, `fruit`, 16 kHz mono, 2.2 to 4.4 s each), concatenated in that order into 15.4 s of real speech. No fixture is long enough alone, so the 5 s and 10 s segments are several questions in a row. Zero padding was not used. For each segment length L, the recognition prompt ran on every prefix of 0.5 s, 1.0 s, up to L, three times each, and the median is reported. The prompt is the lab's (`language English`, digits and no-filler flags, up to 96 tokens). The 10 s case crosses Rumble's 8 s encoder window, and the cost rises smoothly across that boundary.

The draft prompt is a crude system instruction ("one short spoken acknowledgment of at most twelve words, do not answer"), limit 24 tokens, five repeats. It is a timing probe. Its text is not a quality sample, since two drafts answered the question instead of acknowledging it.

## Time per partial (median of three, seconds)

| Prefix | 0.5 | 1.0 | 1.5 | 2.0 | 3.0 | 4.0 | 5.0 | 6.0 | 7.0 | 8.0 | 9.0 | 10.0 |
|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|
| Time | 0.129 | 0.184 | 0.222 | 0.253 | 0.333 | 0.366 | 0.454 | 0.494 | 0.578 | 0.651 | 0.742 | 0.800 |

The three ladders (2 s, 5 s, 10 s) agree within 0.02 s at the same prefix. Cost is roughly 0.1 s plus 0.07 s per second of audio.

## Gap test

| Segment | Partial | Gap to next tick | LFM draft (median, words) | Sentence synthesis, total (first audio) | Serial chain |
|---|---:|---:|---:|---:|---:|
| 2 s | 0.253 s | 0.247 s | 0.26 s (3) | 0.242 s (0.049 s) | 0.73 s |
| 5 s | 0.454 s | 0.046 s | 0.39 s (6) | 0.275 s (0.049 s) | 1.12 s |
| 10 s | 0.800 s | none | 0.42 s (7) | 0.274 s (0.049 s) | 1.50 s |

Neither the draft alone nor the draft plus the first audio of the sentence fits the 2 s gap (0.31 s against 0.25 s). The chain column used each segment's own partial text and draft. Peak `phys_footprint` over the whole run was 529 MB (506 MB when ready).

## What is still unknown

- Whether the pipeline's tick coalesces or queues late partials. Here every step ran alone and back to back.
- Contention if Rumble, LFM2.5 and the voice share the GPU at the same instant. The spec forbids it, so it was not tried.
- Live microphone behavior and real end-of-turn timing.
- The effect of the short-clip padding fix (R2). The partials are the pinned library's output, and the model sometimes switched between digits and words as the prefix grew.
- Draft quality. It needs a real prompt and the code checks from spec section 8.2.
