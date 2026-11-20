# R5b: can each candidate file be fetched without accepting terms?

Checked 2026-11-20. Spec 109 task T005, download half.

## Answer

Every file the app would fetch is open. Only the upstream Kyutai repository is gated.

All three GGUF files in the pack answer without a login. The Kokoro Core ML files, the Phonon 31m Core ML files and the Pocket TTS small-model Core ML files answer without a login too. Each returned a redirect to the Hugging Face CDN and then 200. No file in those repositories returned 401 or 403.

`kyutai/pocket-tts` is the exception. Its model API says `gated: auto` and every weight file returned 401 to an anonymous request. That is the repository that holds Kyutai's own 18M, 7M and 2M weights (`languages/english_*_2026-10/`). FluidAudio does not read from it. FluidAudio reads the Core ML builds in `FluidInference/pocket-tts-coreml`, which is not gated.

Pocket TTS also has an ungated twin, `kyutai/pocket-tts-without-voice-cloning` (`gated: false`). Its README carries the same gate prompt text.

## How this was checked

- **read**: one anonymous HEAD request per file to `https://huggingface.co/<repo>/resolve/<revision>/<path>`. The first status and the status after following one redirect are shown. No body was downloaded.
- **read**: a ranged GET of the first 100 bytes of `Rumble-2-Flash-AD-Q5_K_M.gguf` in the pack repository returned 206.
- **read**: the `gated` field comes from `https://huggingface.co/api/models/<repo>`. File sizes come from the tree API (`lfs.size`).
- A plain generic client string was used. No login, no token, no terms accepted. No personal or project detail was sent.
- A 307 first status was seen on small files and a 302 on large files. I assume the 302 is the CDN hand-off. Both ended in 200.
- 617 files were checked and each has its own row below. The raw results are also in `AstralDeep-wt/voice-bench-109/licences/tools/results.json`.

## What was and was not selected

- Kokoro: `ANE/` (the legacy English chain), `ANE-v3/` English files, the root G2P Core ML folders, the root lexicon and vocabulary JSON files, and `voices/`. The Japanese and Mandarin folders and the `.mlpackage` source folders were left out.
- Pocket TTS and Phonon: `phonon/phonon-31m-en/` and `v4/english_18m_2026-10`, `v4/english_7m_2026-10` and `v4/english_2m_2026-10`. The older `v2` and `v2.1` 100M packs were left out.
- Pack: the three GGUF files from the pack repository and the same files from their source repositories. The two source repositories matched the pack on SHA-256 for all three files (see below).
- I did not check whether FluidAudio fetches every one of these files. The row set is the whole folder for each variant.
- **assumed**: the resolve URL pattern is the one FluidAudio uses. I read `ModelRegistry.resolveModel` calls in the source but did not trace the exact URL.

## The three GGUF files in the pack

| File | Bytes | SHA-256 (from the tree API) | Pack repository | Source repository |
|---|---:|---|---|---|
| `rumble/Rumble-2-Flash-AD-Q5_K_M.gguf` | 283,424,640 | `c49430ce4eb24ca6e6ff154f135c63a0dd98fc2247746824f40d96df7d2bd7ee` | 302 to 200 | AtomicChat repository, same hash |
| `rumble/mmproj-Rumble-2-Flash-Q8_0.gguf` | 214,393,024 | `97116d3656cbe4912ac9aee43eb4a887832121e2a2c07c5ee61617052bdd30f8` | 302 to 200 | AtomicChat repository, same hash |
| `chat-1.2b/LFM2.5-1.2B-Instruct-Q4_K_M.gguf` | 730,895,168 | `b1b3de114215d9507409a662a501a631095a479a419584e8a2ded6304b19b4f5` | 302 to 200 | LiquidAI GGUF repository, same hash |

The byte counts match `research.md` section 3.2 exactly.

## Gated summary

| Repository | `gated` | Result |
|---|---|---|
| `FluidInference/kokoro-82m-coreml` | false | open |
| `FluidInference/pocket-tts-coreml` | false | open, includes Phonon 31m and the v4 small models |
| `gradium/phonon-31m-en` | false | open |
| `Kentucky-Open-Science/Rumble-2-Flash-Astral` | false | open |
| `AtomicChat/Rumble-2-Flash-GGUF` | false | open |
| `LiquidAI/LFM2.5-1.2B-Instruct-GGUF` | false | open |
| `kyutai/pocket-tts` | auto | weights gated (401). README readable through `/resolve/` (200) and not through `/raw/` (401) |
| `kyutai/pocket-tts-without-voice-cloning` | false | open |

## Rows

One row per file, 617 in all. Sorted by repository then path.

| Repository | Revision | Path | Bytes | Status (HEAD, first to final) | Gated |
|---|---|---|---:|---|---|
| FluidInference/kokoro-82m-coreml | `51c072023b254cd02368102ab88bcfbff6cad4cd` | `.gitattributes` | 24,236 | 307 to 200 | false |
| FluidInference/kokoro-82m-coreml | `51c072023b254cd02368102ab88bcfbff6cad4cd` | `ANE-v3/NOTICE` | 1,267 | 307 to 200 | false |
| FluidInference/kokoro-82m-coreml | `51c072023b254cd02368102ab88bcfbff6cad4cd` | `ANE-v3/README.md` | 10,259 | 307 to 200 | false |
| FluidInference/kokoro-82m-coreml | `51c072023b254cd02368102ab88bcfbff6cad4cd` | `ANE-v3/decoder/KokoroDecoderPre_120.mlmodelc/analytics/coremldata.bin` | 243 | 302 to 200 | false |
| FluidInference/kokoro-82m-coreml | `51c072023b254cd02368102ab88bcfbff6cad4cd` | `ANE-v3/decoder/KokoroDecoderPre_120.mlmodelc/coremldata.bin` | 472 | 302 to 200 | false |
| FluidInference/kokoro-82m-coreml | `51c072023b254cd02368102ab88bcfbff6cad4cd` | `ANE-v3/decoder/KokoroDecoderPre_120.mlmodelc/model.mil` | 101,910 | 307 to 200 | false |
| FluidInference/kokoro-82m-coreml | `51c072023b254cd02368102ab88bcfbff6cad4cd` | `ANE-v3/decoder/KokoroDecoderPre_120.mlmodelc/weights/weight.bin` | 33,642,176 | 302 to 200 | false |
| FluidInference/kokoro-82m-coreml | `51c072023b254cd02368102ab88bcfbff6cad4cd` | `ANE-v3/decoder/KokoroDecoderPre_200.mlmodelc/analytics/coremldata.bin` | 243 | 302 to 200 | false |
| FluidInference/kokoro-82m-coreml | `51c072023b254cd02368102ab88bcfbff6cad4cd` | `ANE-v3/decoder/KokoroDecoderPre_200.mlmodelc/coremldata.bin` | 474 | 302 to 200 | false |
| FluidInference/kokoro-82m-coreml | `51c072023b254cd02368102ab88bcfbff6cad4cd` | `ANE-v3/decoder/KokoroDecoderPre_200.mlmodelc/model.mil` | 101,910 | 307 to 200 | false |
| FluidInference/kokoro-82m-coreml | `51c072023b254cd02368102ab88bcfbff6cad4cd` | `ANE-v3/decoder/KokoroDecoderPre_200.mlmodelc/weights/weight.bin` | 33,642,176 | 302 to 200 | false |
| FluidInference/kokoro-82m-coreml | `51c072023b254cd02368102ab88bcfbff6cad4cd` | `ANE-v3/fast/KokoroAlbert_32.mlmodelc/analytics/coremldata.bin` | 243 | 302 to 200 | false |
| FluidInference/kokoro-82m-coreml | `51c072023b254cd02368102ab88bcfbff6cad4cd` | `ANE-v3/fast/KokoroAlbert_32.mlmodelc/coremldata.bin` | 183 | 302 to 200 | false |
| FluidInference/kokoro-82m-coreml | `51c072023b254cd02368102ab88bcfbff6cad4cd` | `ANE-v3/fast/KokoroAlbert_32.mlmodelc/model.mil` | 99,177 | 307 to 200 | false |
| FluidInference/kokoro-82m-coreml | `51c072023b254cd02368102ab88bcfbff6cad4cd` | `ANE-v3/fast/KokoroAlbert_32.mlmodelc/weights/weight.bin` | 5,718,848 | 302 to 200 | false |
| FluidInference/kokoro-82m-coreml | `51c072023b254cd02368102ab88bcfbff6cad4cd` | `ANE-v3/fast/KokoroAlbert_64.mlmodelc/analytics/coremldata.bin` | 243 | 302 to 200 | false |
| FluidInference/kokoro-82m-coreml | `51c072023b254cd02368102ab88bcfbff6cad4cd` | `ANE-v3/fast/KokoroAlbert_64.mlmodelc/coremldata.bin` | 183 | 302 to 200 | false |
| FluidInference/kokoro-82m-coreml | `51c072023b254cd02368102ab88bcfbff6cad4cd` | `ANE-v3/fast/KokoroAlbert_64.mlmodelc/model.mil` | 99,305 | 307 to 200 | false |
| FluidInference/kokoro-82m-coreml | `51c072023b254cd02368102ab88bcfbff6cad4cd` | `ANE-v3/fast/KokoroAlbert_64.mlmodelc/weights/weight.bin` | 5,718,848 | 302 to 200 | false |
| FluidInference/kokoro-82m-coreml | `51c072023b254cd02368102ab88bcfbff6cad4cd` | `ANE-v3/fast/KokoroSourceGenerator.mlmodelc/analytics/coremldata.bin` | 243 | 302 to 200 | false |
| FluidInference/kokoro-82m-coreml | `51c072023b254cd02368102ab88bcfbff6cad4cd` | `ANE-v3/fast/KokoroSourceGenerator.mlmodelc/coremldata.bin` | 752 | 302 to 200 | false |
| FluidInference/kokoro-82m-coreml | `51c072023b254cd02368102ab88bcfbff6cad4cd` | `ANE-v3/fast/KokoroSourceGenerator.mlmodelc/model.mil` | 470,092 | 307 to 200 | false |
| FluidInference/kokoro-82m-coreml | `51c072023b254cd02368102ab88bcfbff6cad4cd` | `ANE-v3/fast/KokoroSourceGenerator.mlmodelc/weights/weight.bin` | 39,353,848 | 302 to 200 | false |
| FluidInference/kokoro-82m-coreml | `51c072023b254cd02368102ab88bcfbff6cad4cd` | `ANE-v3/fast/albert-provenance.json` | 155 | 307 to 200 | false |
| FluidInference/kokoro-82m-coreml | `51c072023b254cd02368102ab88bcfbff6cad4cd` | `ANE-v3/fast/fixtures.json` | 14,774 | 307 to 200 | false |
| FluidInference/kokoro-82m-coreml | `51c072023b254cd02368102ab88bcfbff6cad4cd` | `ANE-v3/fast/fox-audio.f32` | 288,000 | 302 to 200 | false |
| FluidInference/kokoro-82m-coreml | `51c072023b254cd02368102ab88bcfbff6cad4cd` | `ANE-v3/fast/fox-dsp.json` | 7,619 | 307 to 200 | false |
| FluidInference/kokoro-82m-coreml | `51c072023b254cd02368102ab88bcfbff6cad4cd` | `ANE-v3/fast/fox-f0.f32` | 960 | 307 to 200 | false |
| FluidInference/kokoro-82m-coreml | `51c072023b254cd02368102ab88bcfbff6cad4cd` | `ANE-v3/fast/fox-features.f32` | 491,520 | 302 to 200 | false |
| FluidInference/kokoro-82m-coreml | `51c072023b254cd02368102ab88bcfbff6cad4cd` | `ANE-v3/fast/fox-har.f32` | 1,267,288 | 302 to 200 | false |
| FluidInference/kokoro-82m-coreml | `51c072023b254cd02368102ab88bcfbff6cad4cd` | `ANE-v3/fast/fox-source.f32` | 288,000 | 302 to 200 | false |
| FluidInference/kokoro-82m-coreml | `51c072023b254cd02368102ab88bcfbff6cad4cd` | `ANE-v3/fast/generator-provenance.json` | 34,627 | 307 to 200 | false |
| FluidInference/kokoro-82m-coreml | `51c072023b254cd02368102ab88bcfbff6cad4cd` | `ANE-v3/fast/hello-audio.f32` | 156,000 | 302 to 200 | false |
| FluidInference/kokoro-82m-coreml | `51c072023b254cd02368102ab88bcfbff6cad4cd` | `ANE-v3/fast/hello-dsp.json` | 5,481 | 307 to 200 | false |
| FluidInference/kokoro-82m-coreml | `51c072023b254cd02368102ab88bcfbff6cad4cd` | `ANE-v3/fast/hello-f0.f32` | 520 | 307 to 200 | false |
| FluidInference/kokoro-82m-coreml | `51c072023b254cd02368102ab88bcfbff6cad4cd` | `ANE-v3/fast/hello-features.f32` | 266,240 | 302 to 200 | false |
| FluidInference/kokoro-82m-coreml | `51c072023b254cd02368102ab88bcfbff6cad4cd` | `ANE-v3/fast/hello-har.f32` | 686,488 | 302 to 200 | false |
| FluidInference/kokoro-82m-coreml | `51c072023b254cd02368102ab88bcfbff6cad4cd` | `ANE-v3/fast/hello-source.f32` | 156,000 | 302 to 200 | false |
| FluidInference/kokoro-82m-coreml | `51c072023b254cd02368102ab88bcfbff6cad4cd` | `ANE-v3/fast/native-source.json` | 8,340 | 307 to 200 | false |
| FluidInference/kokoro-82m-coreml | `51c072023b254cd02368102ab88bcfbff6cad4cd` | `ANE-v3/manifest.json` | 20,416 | 307 to 200 | false |
| FluidInference/kokoro-82m-coreml | `51c072023b254cd02368102ab88bcfbff6cad4cd` | `ANE-v3/mattmireles-LICENSE` | 11,357 | 307 to 200 | false |
| FluidInference/kokoro-82m-coreml | `51c072023b254cd02368102ab88bcfbff6cad4cd` | `ANE-v3/runtime/Package.swift` | 622 | 307 to 200 | false |
| FluidInference/kokoro-82m-coreml | `51c072023b254cd02368102ab88bcfbff6cad4cd` | `ANE-v3/runtime/Sources/KokoroFast/Arrays.swift` | 7,711 | 307 to 200 | false |
| FluidInference/kokoro-82m-coreml | `51c072023b254cd02368102ab88bcfbff6cad4cd` | `ANE-v3/runtime/Sources/KokoroFast/NativeSource.swift` | 6,394 | 307 to 200 | false |
| FluidInference/kokoro-82m-coreml | `51c072023b254cd02368102ab88bcfbff6cad4cd` | `ANE-v3/runtime/Sources/KokoroFast/Pipeline.swift` | 14,146 | 307 to 200 | false |
| FluidInference/kokoro-82m-coreml | `51c072023b254cd02368102ab88bcfbff6cad4cd` | `ANE-v3/runtime/Sources/KokoroFastBenchmark/main.swift` | 2,049 | 307 to 200 | false |
| FluidInference/kokoro-82m-coreml | `51c072023b254cd02368102ab88bcfbff6cad4cd` | `ANE-v3/runtime/Sources/KokoroFastCheck/main.swift` | 6,021 | 307 to 200 | false |
| FluidInference/kokoro-82m-coreml | `51c072023b254cd02368102ab88bcfbff6cad4cd` | `ANE-v3/samples/fox.wav` | 144,044 | 302 to 200 | false |
| FluidInference/kokoro-82m-coreml | `51c072023b254cd02368102ab88bcfbff6cad4cd` | `ANE-v3/samples/hello.wav` | 78,044 | 307 to 200 | false |
| FluidInference/kokoro-82m-coreml | `51c072023b254cd02368102ab88bcfbff6cad4cd` | `ANE-v3/tools/download-fast-pipeline.py` | 3,868 | 307 to 200 | false |
| FluidInference/kokoro-82m-coreml | `51c072023b254cd02368102ab88bcfbff6cad4cd` | `ANE-v3/tools/prepare-fast-input.py` | 1,916 | 307 to 200 | false |
| FluidInference/kokoro-82m-coreml | `51c072023b254cd02368102ab88bcfbff6cad4cd` | `ANE-v3/tools/validate-fast-pipeline.py` | 4,642 | 307 to 200 | false |
| FluidInference/kokoro-82m-coreml | `51c072023b254cd02368102ab88bcfbff6cad4cd` | `ANE-v3/tools/validate-generator-transfer.py` | 2,084 | 307 to 200 | false |
| FluidInference/kokoro-82m-coreml | `51c072023b254cd02368102ab88bcfbff6cad4cd` | `ANE-v3/validation/kokoro-fast-2026-10-07.json` | 96,849 | 307 to 200 | false |
| FluidInference/kokoro-82m-coreml | `51c072023b254cd02368102ab88bcfbff6cad4cd` | `ANE-v3/validation/kokoro-fast-extra-fixtures.json` | 23,517 | 307 to 200 | false |
| FluidInference/kokoro-82m-coreml | `51c072023b254cd02368102ab88bcfbff6cad4cd` | `ANE-v3/validation/latency.json` | 25,972 | 307 to 200 | false |
| FluidInference/kokoro-82m-coreml | `51c072023b254cd02368102ab88bcfbff6cad4cd` | `ANE-v3/validation/native-checks.json` | 1,240 | 307 to 200 | false |
| FluidInference/kokoro-82m-coreml | `51c072023b254cd02368102ab88bcfbff6cad4cd` | `ANE-v3/validation/native-validation.json` | 658 | 307 to 200 | false |
| FluidInference/kokoro-82m-coreml | `51c072023b254cd02368102ab88bcfbff6cad4cd` | `ANE/.DS_Store` | 10,244 | 307 to 200 | false |
| FluidInference/kokoro-82m-coreml | `51c072023b254cd02368102ab88bcfbff6cad4cd` | `ANE/KokoroAlbert.mlmodelc/analytics/coremldata.bin` | 243 | 302 to 200 | false |
| FluidInference/kokoro-82m-coreml | `51c072023b254cd02368102ab88bcfbff6cad4cd` | `ANE/KokoroAlbert.mlmodelc/coremldata.bin` | 433 | 302 to 200 | false |
| FluidInference/kokoro-82m-coreml | `51c072023b254cd02368102ab88bcfbff6cad4cd` | `ANE/KokoroAlbert.mlmodelc/metadata.json` | 2,480 | 307 to 200 | false |
| FluidInference/kokoro-82m-coreml | `51c072023b254cd02368102ab88bcfbff6cad4cd` | `ANE/KokoroAlbert.mlmodelc/model.mil` | 101,485 | 307 to 200 | false |
| FluidInference/kokoro-82m-coreml | `51c072023b254cd02368102ab88bcfbff6cad4cd` | `ANE/KokoroAlbert.mlmodelc/weights/weight.bin` | 5,718,848 | 302 to 200 | false |
| FluidInference/kokoro-82m-coreml | `51c072023b254cd02368102ab88bcfbff6cad4cd` | `ANE/KokoroAlignment.mlmodelc/analytics/coremldata.bin` | 243 | 302 to 200 | false |
| FluidInference/kokoro-82m-coreml | `51c072023b254cd02368102ab88bcfbff6cad4cd` | `ANE/KokoroAlignment.mlmodelc/coremldata.bin` | 484 | 302 to 200 | false |
| FluidInference/kokoro-82m-coreml | `51c072023b254cd02368102ab88bcfbff6cad4cd` | `ANE/KokoroAlignment.mlmodelc/metadata.json` | 3,021 | 307 to 200 | false |
| FluidInference/kokoro-82m-coreml | `51c072023b254cd02368102ab88bcfbff6cad4cd` | `ANE/KokoroAlignment.mlmodelc/model.mil` | 8,194 | 307 to 200 | false |
| FluidInference/kokoro-82m-coreml | `51c072023b254cd02368102ab88bcfbff6cad4cd` | `ANE/KokoroAlignment.mlmodelc/weights/weight.bin` | 4,128 | 302 to 200 | false |
| FluidInference/kokoro-82m-coreml | `51c072023b254cd02368102ab88bcfbff6cad4cd` | `ANE/KokoroNoise.mlmodelc/analytics/coremldata.bin` | 243 | 302 to 200 | false |
| FluidInference/kokoro-82m-coreml | `51c072023b254cd02368102ab88bcfbff6cad4cd` | `ANE/KokoroNoise.mlmodelc/coremldata.bin` | 440 | 302 to 200 | false |
| FluidInference/kokoro-82m-coreml | `51c072023b254cd02368102ab88bcfbff6cad4cd` | `ANE/KokoroNoise.mlmodelc/metadata.json` | 3,020 | 307 to 200 | false |
| FluidInference/kokoro-82m-coreml | `51c072023b254cd02368102ab88bcfbff6cad4cd` | `ANE/KokoroNoise.mlmodelc/model.mil` | 91,698 | 307 to 200 | false |
| FluidInference/kokoro-82m-coreml | `51c072023b254cd02368102ab88bcfbff6cad4cd` | `ANE/KokoroNoise.mlmodelc/weights/weight.bin` | 4,580,160 | 302 to 200 | false |
| FluidInference/kokoro-82m-coreml | `51c072023b254cd02368102ab88bcfbff6cad4cd` | `ANE/KokoroNoise_v2.mlmodelc/analytics/coremldata.bin` | 243 | 302 to 200 | false |
| FluidInference/kokoro-82m-coreml | `51c072023b254cd02368102ab88bcfbff6cad4cd` | `ANE/KokoroNoise_v2.mlmodelc/coremldata.bin` | 440 | 302 to 200 | false |
| FluidInference/kokoro-82m-coreml | `51c072023b254cd02368102ab88bcfbff6cad4cd` | `ANE/KokoroNoise_v2.mlmodelc/metadata.json` | 3,020 | 307 to 200 | false |
| FluidInference/kokoro-82m-coreml | `51c072023b254cd02368102ab88bcfbff6cad4cd` | `ANE/KokoroNoise_v2.mlmodelc/model.mil` | 93,152 | 307 to 200 | false |
| FluidInference/kokoro-82m-coreml | `51c072023b254cd02368102ab88bcfbff6cad4cd` | `ANE/KokoroNoise_v2.mlmodelc/weights/weight.bin` | 4,580,160 | 302 to 200 | false |
| FluidInference/kokoro-82m-coreml | `51c072023b254cd02368102ab88bcfbff6cad4cd` | `ANE/KokoroPostAlbert.mlmodelc/analytics/coremldata.bin` | 243 | 302 to 200 | false |
| FluidInference/kokoro-82m-coreml | `51c072023b254cd02368102ab88bcfbff6cad4cd` | `ANE/KokoroPostAlbert.mlmodelc/coremldata.bin` | 556 | 302 to 200 | false |
| FluidInference/kokoro-82m-coreml | `51c072023b254cd02368102ab88bcfbff6cad4cd` | `ANE/KokoroPostAlbert.mlmodelc/metadata.json` | 4,171 | 307 to 200 | false |
| FluidInference/kokoro-82m-coreml | `51c072023b254cd02368102ab88bcfbff6cad4cd` | `ANE/KokoroPostAlbert.mlmodelc/model.mil` | 60,047 | 307 to 200 | false |
| FluidInference/kokoro-82m-coreml | `51c072023b254cd02368102ab88bcfbff6cad4cd` | `ANE/KokoroPostAlbert.mlmodelc/weights/weight.bin` | 13,806,464 | 302 to 200 | false |
| FluidInference/kokoro-82m-coreml | `51c072023b254cd02368102ab88bcfbff6cad4cd` | `ANE/KokoroProsody.mlmodelc/analytics/coremldata.bin` | 243 | 302 to 200 | false |
| FluidInference/kokoro-82m-coreml | `51c072023b254cd02368102ab88bcfbff6cad4cd` | `ANE/KokoroProsody.mlmodelc/coremldata.bin` | 421 | 302 to 200 | false |
| FluidInference/kokoro-82m-coreml | `51c072023b254cd02368102ab88bcfbff6cad4cd` | `ANE/KokoroProsody.mlmodelc/metadata.json` | 2,721 | 307 to 200 | false |
| FluidInference/kokoro-82m-coreml | `51c072023b254cd02368102ab88bcfbff6cad4cd` | `ANE/KokoroProsody.mlmodelc/model.mil` | 79,705 | 307 to 200 | false |
| FluidInference/kokoro-82m-coreml | `51c072023b254cd02368102ab88bcfbff6cad4cd` | `ANE/KokoroProsody.mlmodelc/weights/weight.bin` | 8,454,272 | 302 to 200 | false |
| FluidInference/kokoro-82m-coreml | `51c072023b254cd02368102ab88bcfbff6cad4cd` | `ANE/KokoroProsody_v2.mlmodelc/analytics/coremldata.bin` | 243 | 302 to 200 | false |
| FluidInference/kokoro-82m-coreml | `51c072023b254cd02368102ab88bcfbff6cad4cd` | `ANE/KokoroProsody_v2.mlmodelc/coremldata.bin` | 421 | 302 to 200 | false |
| FluidInference/kokoro-82m-coreml | `51c072023b254cd02368102ab88bcfbff6cad4cd` | `ANE/KokoroProsody_v2.mlmodelc/model.mil` | 73,353 | 307 to 200 | false |
| FluidInference/kokoro-82m-coreml | `51c072023b254cd02368102ab88bcfbff6cad4cd` | `ANE/KokoroProsody_v2.mlmodelc/weights/weight.bin` | 8,513,664 | 302 to 200 | false |
| FluidInference/kokoro-82m-coreml | `51c072023b254cd02368102ab88bcfbff6cad4cd` | `ANE/KokoroTail.mlmodelc/analytics/coremldata.bin` | 243 | 302 to 200 | false |
| FluidInference/kokoro-82m-coreml | `51c072023b254cd02368102ab88bcfbff6cad4cd` | `ANE/KokoroTail.mlmodelc/coremldata.bin` | 392 | 302 to 200 | false |
| FluidInference/kokoro-82m-coreml | `51c072023b254cd02368102ab88bcfbff6cad4cd` | `ANE/KokoroTail.mlmodelc/metadata.json` | 1,872 | 307 to 200 | false |
| FluidInference/kokoro-82m-coreml | `51c072023b254cd02368102ab88bcfbff6cad4cd` | `ANE/KokoroTail.mlmodelc/model.mil` | 7,014 | 307 to 200 | false |
| FluidInference/kokoro-82m-coreml | `51c072023b254cd02368102ab88bcfbff6cad4cd` | `ANE/KokoroTail.mlmodelc/weights/weight.bin` | 81,088 | 302 to 200 | false |
| FluidInference/kokoro-82m-coreml | `51c072023b254cd02368102ab88bcfbff6cad4cd` | `ANE/KokoroTail_v2.mlmodelc/analytics/coremldata.bin` | 243 | 302 to 200 | false |
| FluidInference/kokoro-82m-coreml | `51c072023b254cd02368102ab88bcfbff6cad4cd` | `ANE/KokoroTail_v2.mlmodelc/coremldata.bin` | 392 | 302 to 200 | false |
| FluidInference/kokoro-82m-coreml | `51c072023b254cd02368102ab88bcfbff6cad4cd` | `ANE/KokoroTail_v2.mlmodelc/metadata.json` | 1,872 | 307 to 200 | false |
| FluidInference/kokoro-82m-coreml | `51c072023b254cd02368102ab88bcfbff6cad4cd` | `ANE/KokoroTail_v2.mlmodelc/model.mil` | 7,014 | 307 to 200 | false |
| FluidInference/kokoro-82m-coreml | `51c072023b254cd02368102ab88bcfbff6cad4cd` | `ANE/KokoroTail_v2.mlmodelc/weights/weight.bin` | 81,088 | 302 to 200 | false |
| FluidInference/kokoro-82m-coreml | `51c072023b254cd02368102ab88bcfbff6cad4cd` | `ANE/KokoroVocoder.mlmodelc/analytics/coremldata.bin` | 243 | 302 to 200 | false |
| FluidInference/kokoro-82m-coreml | `51c072023b254cd02368102ab88bcfbff6cad4cd` | `ANE/KokoroVocoder.mlmodelc/coremldata.bin` | 626 | 302 to 200 | false |
| FluidInference/kokoro-82m-coreml | `51c072023b254cd02368102ab88bcfbff6cad4cd` | `ANE/KokoroVocoder.mlmodelc/metadata.json` | 4,331 | 307 to 200 | false |
| FluidInference/kokoro-82m-coreml | `51c072023b254cd02368102ab88bcfbff6cad4cd` | `ANE/KokoroVocoder.mlmodelc/model.mil` | 309,000 | 307 to 200 | false |
| FluidInference/kokoro-82m-coreml | `51c072023b254cd02368102ab88bcfbff6cad4cd` | `ANE/KokoroVocoder.mlmodelc/weights/weight.bin` | 48,889,920 | 302 to 200 | false |
| FluidInference/kokoro-82m-coreml | `51c072023b254cd02368102ab88bcfbff6cad4cd` | `ANE/KokoroVocoder_v2.mlmodelc/analytics/coremldata.bin` | 243 | 302 to 200 | false |
| FluidInference/kokoro-82m-coreml | `51c072023b254cd02368102ab88bcfbff6cad4cd` | `ANE/KokoroVocoder_v2.mlmodelc/coremldata.bin` | 628 | 302 to 200 | false |
| FluidInference/kokoro-82m-coreml | `51c072023b254cd02368102ab88bcfbff6cad4cd` | `ANE/KokoroVocoder_v2.mlmodelc/metadata.json` | 4,416 | 307 to 200 | false |
| FluidInference/kokoro-82m-coreml | `51c072023b254cd02368102ab88bcfbff6cad4cd` | `ANE/KokoroVocoder_v2.mlmodelc/model.mil` | 277,524 | 307 to 200 | false |
| FluidInference/kokoro-82m-coreml | `51c072023b254cd02368102ab88bcfbff6cad4cd` | `ANE/KokoroVocoder_v2.mlmodelc/weights/weight.bin` | 49,084,224 | 302 to 200 | false |
| FluidInference/kokoro-82m-coreml | `51c072023b254cd02368102ab88bcfbff6cad4cd` | `ANE/LICENSE` | 11,339 | 307 to 200 | false |
| FluidInference/kokoro-82m-coreml | `51c072023b254cd02368102ab88bcfbff6cad4cd` | `ANE/README.md` | 4,813 | 307 to 200 | false |
| FluidInference/kokoro-82m-coreml | `51c072023b254cd02368102ab88bcfbff6cad4cd` | `ANE/af_heart.bin` | 522,240 | 302 to 200 | false |
| FluidInference/kokoro-82m-coreml | `51c072023b254cd02368102ab88bcfbff6cad4cd` | `ANE/vocab.json` | 1,416 | 307 to 200 | false |
| FluidInference/kokoro-82m-coreml | `51c072023b254cd02368102ab88bcfbff6cad4cd` | `G2PDecoder.mlmodelc/analytics/coremldata.bin` | 243 | 302 to 200 | false |
| FluidInference/kokoro-82m-coreml | `51c072023b254cd02368102ab88bcfbff6cad4cd` | `G2PDecoder.mlmodelc/coremldata.bin` | 545 | 302 to 200 | false |
| FluidInference/kokoro-82m-coreml | `51c072023b254cd02368102ab88bcfbff6cad4cd` | `G2PDecoder.mlmodelc/metadata.json` | 3,064 | 307 to 200 | false |
| FluidInference/kokoro-82m-coreml | `51c072023b254cd02368102ab88bcfbff6cad4cd` | `G2PDecoder.mlmodelc/model.mil` | 19,737 | 307 to 200 | false |
| FluidInference/kokoro-82m-coreml | `51c072023b254cd02368102ab88bcfbff6cad4cd` | `G2PDecoder.mlmodelc/weights/weight.bin` | 828,030 | 302 to 200 | false |
| FluidInference/kokoro-82m-coreml | `51c072023b254cd02368102ab88bcfbff6cad4cd` | `G2PEncoder.mlmodelc/analytics/coremldata.bin` | 243 | 302 to 200 | false |
| FluidInference/kokoro-82m-coreml | `51c072023b254cd02368102ab88bcfbff6cad4cd` | `G2PEncoder.mlmodelc/coremldata.bin` | 398 | 302 to 200 | false |
| FluidInference/kokoro-82m-coreml | `51c072023b254cd02368102ab88bcfbff6cad4cd` | `G2PEncoder.mlmodelc/metadata.json` | 2,034 | 307 to 200 | false |
| FluidInference/kokoro-82m-coreml | `51c072023b254cd02368102ab88bcfbff6cad4cd` | `G2PEncoder.mlmodelc/model.mil` | 20,392 | 307 to 200 | false |
| FluidInference/kokoro-82m-coreml | `51c072023b254cd02368102ab88bcfbff6cad4cd` | `G2PEncoder.mlmodelc/weights/weight.bin` | 694,592 | 302 to 200 | false |
| FluidInference/kokoro-82m-coreml | `51c072023b254cd02368102ab88bcfbff6cad4cd` | `MultilingualG2PDecoder.mlmodelc/analytics/coremldata.bin` | 243 | 302 to 200 | false |
| FluidInference/kokoro-82m-coreml | `51c072023b254cd02368102ab88bcfbff6cad4cd` | `MultilingualG2PDecoder.mlmodelc/coremldata.bin` | 507 | 302 to 200 | false |
| FluidInference/kokoro-82m-coreml | `51c072023b254cd02368102ab88bcfbff6cad4cd` | `MultilingualG2PDecoder.mlmodelc/metadata.json` | 3,124 | 307 to 200 | false |
| FluidInference/kokoro-82m-coreml | `51c072023b254cd02368102ab88bcfbff6cad4cd` | `MultilingualG2PDecoder.mlmodelc/model.mil` | 99,325 | 307 to 200 | false |
| FluidInference/kokoro-82m-coreml | `51c072023b254cd02368102ab88bcfbff6cad4cd` | `MultilingualG2PDecoder.mlmodelc/weights/weight.bin` | 25,977,088 | 302 to 200 | false |
| FluidInference/kokoro-82m-coreml | `51c072023b254cd02368102ab88bcfbff6cad4cd` | `MultilingualG2PEncoder.mlmodelc/analytics/coremldata.bin` | 243 | 302 to 200 | false |
| FluidInference/kokoro-82m-coreml | `51c072023b254cd02368102ab88bcfbff6cad4cd` | `MultilingualG2PEncoder.mlmodelc/coremldata.bin` | 439 | 302 to 200 | false |
| FluidInference/kokoro-82m-coreml | `51c072023b254cd02368102ab88bcfbff6cad4cd` | `MultilingualG2PEncoder.mlmodelc/metadata.json` | 2,654 | 307 to 200 | false |
| FluidInference/kokoro-82m-coreml | `51c072023b254cd02368102ab88bcfbff6cad4cd` | `MultilingualG2PEncoder.mlmodelc/model.mil` | 153,849 | 307 to 200 | false |
| FluidInference/kokoro-82m-coreml | `51c072023b254cd02368102ab88bcfbff6cad4cd` | `MultilingualG2PEncoder.mlmodelc/weights/weight.bin` | 57,056,704 | 302 to 200 | false |
| FluidInference/kokoro-82m-coreml | `51c072023b254cd02368102ab88bcfbff6cad4cd` | `README.md` | 11,578 | 307 to 200 | false |
| FluidInference/kokoro-82m-coreml | `51c072023b254cd02368102ab88bcfbff6cad4cd` | `config.json` | 2 | 307 to 200 | false |
| FluidInference/kokoro-82m-coreml | `51c072023b254cd02368102ab88bcfbff6cad4cd` | `es_lexicon_cache.json` | 3,894,233 | 307 to 200 | false |
| FluidInference/kokoro-82m-coreml | `51c072023b254cd02368102ab88bcfbff6cad4cd` | `fr_lexicon_cache.json` | 13,625,891 | 302 to 200 | false |
| FluidInference/kokoro-82m-coreml | `51c072023b254cd02368102ab88bcfbff6cad4cd` | `g2p_vocab.json` | 1,665 | 307 to 200 | false |
| FluidInference/kokoro-82m-coreml | `51c072023b254cd02368102ab88bcfbff6cad4cd` | `gb_gold.json` | 2,838,552 | 307 to 200 | false |
| FluidInference/kokoro-82m-coreml | `51c072023b254cd02368102ab88bcfbff6cad4cd` | `gb_lexicon_cache.json` | 11,172,624 | 302 to 200 | false |
| FluidInference/kokoro-82m-coreml | `51c072023b254cd02368102ab88bcfbff6cad4cd` | `gb_silver.json` | 3,663,898 | 307 to 200 | false |
| FluidInference/kokoro-82m-coreml | `51c072023b254cd02368102ab88bcfbff6cad4cd` | `us_gold.json` | 3,001,196 | 307 to 200 | false |
| FluidInference/kokoro-82m-coreml | `51c072023b254cd02368102ab88bcfbff6cad4cd` | `us_lexicon_cache.json` | 10,444,631 | 302 to 200 | false |
| FluidInference/kokoro-82m-coreml | `51c072023b254cd02368102ab88bcfbff6cad4cd` | `us_silver.json` | 3,105,352 | 307 to 200 | false |
| FluidInference/kokoro-82m-coreml | `51c072023b254cd02368102ab88bcfbff6cad4cd` | `vocab_index.json` | 2,034 | 307 to 200 | false |
| FluidInference/kokoro-82m-coreml | `51c072023b254cd02368102ab88bcfbff6cad4cd` | `voices/af_alloy.json` | 2,668,823 | 307 to 200 | false |
| FluidInference/kokoro-82m-coreml | `51c072023b254cd02368102ab88bcfbff6cad4cd` | `voices/af_aoede.json` | 2,675,629 | 307 to 200 | false |
| FluidInference/kokoro-82m-coreml | `51c072023b254cd02368102ab88bcfbff6cad4cd` | `voices/af_bella.json` | 2,678,538 | 307 to 200 | false |
| FluidInference/kokoro-82m-coreml | `51c072023b254cd02368102ab88bcfbff6cad4cd` | `voices/af_heart.json` | 2,813,032 | 307 to 200 | false |
| FluidInference/kokoro-82m-coreml | `51c072023b254cd02368102ab88bcfbff6cad4cd` | `voices/af_jessica.json` | 2,667,415 | 307 to 200 | false |
| FluidInference/kokoro-82m-coreml | `51c072023b254cd02368102ab88bcfbff6cad4cd` | `voices/af_kore.json` | 2,677,872 | 307 to 200 | false |
| FluidInference/kokoro-82m-coreml | `51c072023b254cd02368102ab88bcfbff6cad4cd` | `voices/af_nicole.json` | 2,680,162 | 307 to 200 | false |
| FluidInference/kokoro-82m-coreml | `51c072023b254cd02368102ab88bcfbff6cad4cd` | `voices/af_nova.json` | 2,677,525 | 307 to 200 | false |
| FluidInference/kokoro-82m-coreml | `51c072023b254cd02368102ab88bcfbff6cad4cd` | `voices/af_river.json` | 2,672,330 | 307 to 200 | false |
| FluidInference/kokoro-82m-coreml | `51c072023b254cd02368102ab88bcfbff6cad4cd` | `voices/af_sarah.json` | 2,672,574 | 307 to 200 | false |
| FluidInference/kokoro-82m-coreml | `51c072023b254cd02368102ab88bcfbff6cad4cd` | `voices/af_sky.json` | 2,677,239 | 307 to 200 | false |
| FluidInference/kokoro-82m-coreml | `51c072023b254cd02368102ab88bcfbff6cad4cd` | `voices/am_adam.json` | 2,675,095 | 307 to 200 | false |
| FluidInference/kokoro-82m-coreml | `51c072023b254cd02368102ab88bcfbff6cad4cd` | `voices/am_echo.json` | 2,674,446 | 307 to 200 | false |
| FluidInference/kokoro-82m-coreml | `51c072023b254cd02368102ab88bcfbff6cad4cd` | `voices/am_eric.json` | 2,661,872 | 307 to 200 | false |
| FluidInference/kokoro-82m-coreml | `51c072023b254cd02368102ab88bcfbff6cad4cd` | `voices/am_fenrir.json` | 2,678,091 | 307 to 200 | false |
| FluidInference/kokoro-82m-coreml | `51c072023b254cd02368102ab88bcfbff6cad4cd` | `voices/am_liam.json` | 2,672,109 | 307 to 200 | false |
| FluidInference/kokoro-82m-coreml | `51c072023b254cd02368102ab88bcfbff6cad4cd` | `voices/am_michael.json` | 2,667,552 | 307 to 200 | false |
| FluidInference/kokoro-82m-coreml | `51c072023b254cd02368102ab88bcfbff6cad4cd` | `voices/am_onyx.json` | 2,668,384 | 307 to 200 | false |
| FluidInference/kokoro-82m-coreml | `51c072023b254cd02368102ab88bcfbff6cad4cd` | `voices/am_puck.json` | 2,671,969 | 307 to 200 | false |
| FluidInference/kokoro-82m-coreml | `51c072023b254cd02368102ab88bcfbff6cad4cd` | `voices/am_santa.json` | 2,679,608 | 307 to 200 | false |
| FluidInference/kokoro-82m-coreml | `51c072023b254cd02368102ab88bcfbff6cad4cd` | `voices/bf_alice.json` | 2,661,239 | 307 to 200 | false |
| FluidInference/kokoro-82m-coreml | `51c072023b254cd02368102ab88bcfbff6cad4cd` | `voices/bf_emma.json` | 2,679,667 | 307 to 200 | false |
| FluidInference/kokoro-82m-coreml | `51c072023b254cd02368102ab88bcfbff6cad4cd` | `voices/bf_isabella.json` | 2,675,405 | 307 to 200 | false |
| FluidInference/kokoro-82m-coreml | `51c072023b254cd02368102ab88bcfbff6cad4cd` | `voices/bf_lily.json` | 2,663,734 | 307 to 200 | false |
| FluidInference/kokoro-82m-coreml | `51c072023b254cd02368102ab88bcfbff6cad4cd` | `voices/bm_daniel.json` | 2,672,663 | 307 to 200 | false |
| FluidInference/kokoro-82m-coreml | `51c072023b254cd02368102ab88bcfbff6cad4cd` | `voices/bm_fable.json` | 2,656,216 | 307 to 200 | false |
| FluidInference/kokoro-82m-coreml | `51c072023b254cd02368102ab88bcfbff6cad4cd` | `voices/bm_george.json` | 2,669,308 | 307 to 200 | false |
| FluidInference/kokoro-82m-coreml | `51c072023b254cd02368102ab88bcfbff6cad4cd` | `voices/bm_lewis.json` | 2,668,483 | 307 to 200 | false |
| FluidInference/kokoro-82m-coreml | `51c072023b254cd02368102ab88bcfbff6cad4cd` | `voices/ef_dora.json` | 2,686,508 | 307 to 200 | false |
| FluidInference/kokoro-82m-coreml | `51c072023b254cd02368102ab88bcfbff6cad4cd` | `voices/em_alex.json` | 2,686,460 | 307 to 200 | false |
| FluidInference/kokoro-82m-coreml | `51c072023b254cd02368102ab88bcfbff6cad4cd` | `voices/em_santa.json` | 2,682,626 | 307 to 200 | false |
| FluidInference/kokoro-82m-coreml | `51c072023b254cd02368102ab88bcfbff6cad4cd` | `voices/ff_siwis.json` | 2,672,012 | 307 to 200 | false |
| FluidInference/kokoro-82m-coreml | `51c072023b254cd02368102ab88bcfbff6cad4cd` | `voices/hf_alpha.json` | 2,675,304 | 307 to 200 | false |
| FluidInference/kokoro-82m-coreml | `51c072023b254cd02368102ab88bcfbff6cad4cd` | `voices/hf_beta.json` | 2,670,179 | 307 to 200 | false |
| FluidInference/kokoro-82m-coreml | `51c072023b254cd02368102ab88bcfbff6cad4cd` | `voices/hm_omega.json` | 2,677,201 | 307 to 200 | false |
| FluidInference/kokoro-82m-coreml | `51c072023b254cd02368102ab88bcfbff6cad4cd` | `voices/hm_psi.json` | 2,667,766 | 307 to 200 | false |
| FluidInference/kokoro-82m-coreml | `51c072023b254cd02368102ab88bcfbff6cad4cd` | `voices/if_sara.json` | 2,668,488 | 307 to 200 | false |
| FluidInference/kokoro-82m-coreml | `51c072023b254cd02368102ab88bcfbff6cad4cd` | `voices/im_nicola.json` | 2,652,608 | 307 to 200 | false |
| FluidInference/kokoro-82m-coreml | `51c072023b254cd02368102ab88bcfbff6cad4cd` | `voices/jf_alpha.json` | 2,668,652 | 307 to 200 | false |
| FluidInference/kokoro-82m-coreml | `51c072023b254cd02368102ab88bcfbff6cad4cd` | `voices/jf_gongitsune.json` | 2,666,403 | 307 to 200 | false |
| FluidInference/kokoro-82m-coreml | `51c072023b254cd02368102ab88bcfbff6cad4cd` | `voices/jf_nezumi.json` | 2,669,419 | 307 to 200 | false |
| FluidInference/kokoro-82m-coreml | `51c072023b254cd02368102ab88bcfbff6cad4cd` | `voices/jf_tebukuro.json` | 2,656,119 | 307 to 200 | false |
| FluidInference/kokoro-82m-coreml | `51c072023b254cd02368102ab88bcfbff6cad4cd` | `voices/jm_kumo.json` | 2,670,045 | 307 to 200 | false |
| FluidInference/kokoro-82m-coreml | `51c072023b254cd02368102ab88bcfbff6cad4cd` | `voices/pf_dora.json` | 2,688,823 | 307 to 200 | false |
| FluidInference/kokoro-82m-coreml | `51c072023b254cd02368102ab88bcfbff6cad4cd` | `voices/pm_alex.json` | 2,685,661 | 307 to 200 | false |
| FluidInference/kokoro-82m-coreml | `51c072023b254cd02368102ab88bcfbff6cad4cd` | `voices/pm_santa.json` | 2,681,608 | 307 to 200 | false |
| FluidInference/kokoro-82m-coreml | `51c072023b254cd02368102ab88bcfbff6cad4cd` | `voices/zf_xiaobei.json` | 2,665,260 | 307 to 200 | false |
| FluidInference/kokoro-82m-coreml | `51c072023b254cd02368102ab88bcfbff6cad4cd` | `voices/zf_xiaoni.json` | 2,665,177 | 307 to 200 | false |
| FluidInference/kokoro-82m-coreml | `51c072023b254cd02368102ab88bcfbff6cad4cd` | `voices/zf_xiaoxiao.json` | 2,667,942 | 307 to 200 | false |
| FluidInference/kokoro-82m-coreml | `51c072023b254cd02368102ab88bcfbff6cad4cd` | `voices/zf_xiaoyi.json` | 2,670,364 | 307 to 200 | false |
| FluidInference/kokoro-82m-coreml | `51c072023b254cd02368102ab88bcfbff6cad4cd` | `voices/zm_yunjian.json` | 2,668,065 | 307 to 200 | false |
| FluidInference/kokoro-82m-coreml | `51c072023b254cd02368102ab88bcfbff6cad4cd` | `voices/zm_yunxi.json` | 2,674,889 | 307 to 200 | false |
| FluidInference/kokoro-82m-coreml | `51c072023b254cd02368102ab88bcfbff6cad4cd` | `voices/zm_yunxia.json` | 2,665,973 | 307 to 200 | false |
| FluidInference/kokoro-82m-coreml | `51c072023b254cd02368102ab88bcfbff6cad4cd` | `voices/zm_yunyang.json` | 2,666,163 | 307 to 200 | false |
| FluidInference/pocket-tts-coreml | `c2e998e269e4d4b51f74061faddb8450cd8f9a9a` | `phonon/phonon-31m-en/LICENSE` | 11,357 | 307 to 200 | false |
| FluidInference/pocket-tts-coreml | `c2e998e269e4d4b51f74061faddb8450cd8f9a9a` | `phonon/phonon-31m-en/README.md` | 2,767 | 307 to 200 | false |
| FluidInference/pocket-tts-coreml | `c2e998e269e4d4b51f74061faddb8450cd8f9a9a` | `phonon/phonon-31m-en/arch.json` | 811 | 307 to 200 | false |
| FluidInference/pocket-tts-coreml | `c2e998e269e4d4b51f74061faddb8450cd8f9a9a` | `phonon/phonon-31m-en/cond_prefill.mlmodelc/analytics/coremldata.bin` | 243 | 302 to 200 | false |
| FluidInference/pocket-tts-coreml | `c2e998e269e4d4b51f74061faddb8450cd8f9a9a` | `phonon/phonon-31m-en/cond_prefill.mlmodelc/coremldata.bin` | 1,389 | 302 to 200 | false |
| FluidInference/pocket-tts-coreml | `c2e998e269e4d4b51f74061faddb8450cd8f9a9a` | `phonon/phonon-31m-en/cond_prefill.mlmodelc/model.mil` | 111,473 | 307 to 200 | false |
| FluidInference/pocket-tts-coreml | `c2e998e269e4d4b51f74061faddb8450cd8f9a9a` | `phonon/phonon-31m-en/cond_prefill.mlmodelc/weights/weight.bin` | 66,260,608 | 302 to 200 | false |
| FluidInference/pocket-tts-coreml | `c2e998e269e4d4b51f74061faddb8450cd8f9a9a` | `phonon/phonon-31m-en/constants/arch.json` | 811 | 307 to 200 | false |
| FluidInference/pocket-tts-coreml | `c2e998e269e4d4b51f74061faddb8450cd8f9a9a` | `phonon/phonon-31m-en/constants/bos_emb.npy` | 256 | 302 to 200 | false |
| FluidInference/pocket-tts-coreml | `c2e998e269e4d4b51f74061faddb8450cd8f9a9a` | `phonon/phonon-31m-en/constants/emb_mean.npy` | 256 | 302 to 200 | false |
| FluidInference/pocket-tts-coreml | `c2e998e269e4d4b51f74061faddb8450cd8f9a9a` | `phonon/phonon-31m-en/constants/emb_std.npy` | 256 | 302 to 200 | false |
| FluidInference/pocket-tts-coreml | `c2e998e269e4d4b51f74061faddb8450cd8f9a9a` | `phonon/phonon-31m-en/constants/input_bias.npy` | 2,176 | 302 to 200 | false |
| FluidInference/pocket-tts-coreml | `c2e998e269e4d4b51f74061faddb8450cd8f9a9a` | `phonon/phonon-31m-en/constants/mimi_state_spec.json` | 2,402 | 307 to 200 | false |
| FluidInference/pocket-tts-coreml | `c2e998e269e4d4b51f74061faddb8450cd8f9a9a` | `phonon/phonon-31m-en/constants/speaker_proj_weight.npy` | 1,048,704 | 302 to 200 | false |
| FluidInference/pocket-tts-coreml | `c2e998e269e4d4b51f74061faddb8450cd8f9a9a` | `phonon/phonon-31m-en/constants/text_embed_table.npy` | 8,194,176 | 302 to 200 | false |
| FluidInference/pocket-tts-coreml | `c2e998e269e4d4b51f74061faddb8450cd8f9a9a` | `phonon/phonon-31m-en/constants_bin/bos_emb.bin` | 128 | 302 to 200 | false |
| FluidInference/pocket-tts-coreml | `c2e998e269e4d4b51f74061faddb8450cd8f9a9a` | `phonon/phonon-31m-en/constants_bin/default.safetensors` | 3,073,840 | 302 to 200 | false |
| FluidInference/pocket-tts-coreml | `c2e998e269e4d4b51f74061faddb8450cd8f9a9a` | `phonon/phonon-31m-en/constants_bin/en_gb_Maeve.safetensors` | 3,073,840 | 302 to 200 | false |
| FluidInference/pocket-tts-coreml | `c2e998e269e4d4b51f74061faddb8450cd8f9a9a` | `phonon/phonon-31m-en/constants_bin/en_us_Easton.safetensors` | 3,073,840 | 302 to 200 | false |
| FluidInference/pocket-tts-coreml | `c2e998e269e4d4b51f74061faddb8450cd8f9a9a` | `phonon/phonon-31m-en/constants_bin/en_us_Zoey.safetensors` | 3,073,840 | 302 to 200 | false |
| FluidInference/pocket-tts-coreml | `c2e998e269e4d4b51f74061faddb8450cd8f9a9a` | `phonon/phonon-31m-en/constants_bin/input_bias.bin` | 2,048 | 302 to 200 | false |
| FluidInference/pocket-tts-coreml | `c2e998e269e4d4b51f74061faddb8450cd8f9a9a` | `phonon/phonon-31m-en/constants_bin/speaker_proj_weight.bin` | 1,048,576 | 302 to 200 | false |
| FluidInference/pocket-tts-coreml | `c2e998e269e4d4b51f74061faddb8450cd8f9a9a` | `phonon/phonon-31m-en/constants_bin/text_embed_table.bin` | 8,194,048 | 302 to 200 | false |
| FluidInference/pocket-tts-coreml | `c2e998e269e4d4b51f74061faddb8450cd8f9a9a` | `phonon/phonon-31m-en/constants_bin/tokenizer.json` | 277,967 | 307 to 200 | false |
| FluidInference/pocket-tts-coreml | `c2e998e269e4d4b51f74061faddb8450cd8f9a9a` | `phonon/phonon-31m-en/constants_bin/tokenizer.model` | 65,045 | 302 to 200 | false |
| FluidInference/pocket-tts-coreml | `c2e998e269e4d4b51f74061faddb8450cd8f9a9a` | `phonon/phonon-31m-en/flow_decoder_fused.mlmodelc/analytics/coremldata.bin` | 243 | 302 to 200 | false |
| FluidInference/pocket-tts-coreml | `c2e998e269e4d4b51f74061faddb8450cd8f9a9a` | `phonon/phonon-31m-en/flow_decoder_fused.mlmodelc/coremldata.bin` | 413 | 302 to 200 | false |
| FluidInference/pocket-tts-coreml | `c2e998e269e4d4b51f74061faddb8450cd8f9a9a` | `phonon/phonon-31m-en/flow_decoder_fused.mlmodelc/model.mil` | 50,870 | 307 to 200 | false |
| FluidInference/pocket-tts-coreml | `c2e998e269e4d4b51f74061faddb8450cd8f9a9a` | `phonon/phonon-31m-en/flow_decoder_fused.mlmodelc/weights/weight.bin` | 37,466,944 | 302 to 200 | false |
| FluidInference/pocket-tts-coreml | `c2e998e269e4d4b51f74061faddb8450cd8f9a9a` | `phonon/phonon-31m-en/flowlm_step.mlmodelc/analytics/coremldata.bin` | 243 | 302 to 200 | false |
| FluidInference/pocket-tts-coreml | `c2e998e269e4d4b51f74061faddb8450cd8f9a9a` | `phonon/phonon-31m-en/flowlm_step.mlmodelc/coremldata.bin` | 1,415 | 302 to 200 | false |
| FluidInference/pocket-tts-coreml | `c2e998e269e4d4b51f74061faddb8450cd8f9a9a` | `phonon/phonon-31m-en/flowlm_step.mlmodelc/model.mil` | 129,026 | 307 to 200 | false |
| FluidInference/pocket-tts-coreml | `c2e998e269e4d4b51f74061faddb8450cd8f9a9a` | `phonon/phonon-31m-en/flowlm_step.mlmodelc/weights/weight.bin` | 75,776,000 | 302 to 200 | false |
| FluidInference/pocket-tts-coreml | `c2e998e269e4d4b51f74061faddb8450cd8f9a9a` | `phonon/phonon-31m-en/manifest.json` | 5,328 | 307 to 200 | false |
| FluidInference/pocket-tts-coreml | `c2e998e269e4d4b51f74061faddb8450cd8f9a9a` | `phonon/phonon-31m-en/mimi_decoder.mlmodelc/analytics/coremldata.bin` | 243 | 302 to 200 | false |
| FluidInference/pocket-tts-coreml | `c2e998e269e4d4b51f74061faddb8450cd8f9a9a` | `phonon/phonon-31m-en/mimi_decoder.mlmodelc/coremldata.bin` | 1,852 | 302 to 200 | false |
| FluidInference/pocket-tts-coreml | `c2e998e269e4d4b51f74061faddb8450cd8f9a9a` | `phonon/phonon-31m-en/mimi_decoder.mlmodelc/model.mil` | 95,070 | 307 to 200 | false |
| FluidInference/pocket-tts-coreml | `c2e998e269e4d4b51f74061faddb8450cd8f9a9a` | `phonon/phonon-31m-en/mimi_decoder.mlmodelc/weights/weight.bin` | 44,897,216 | 302 to 200 | false |
| FluidInference/pocket-tts-coreml | `c2e998e269e4d4b51f74061faddb8450cd8f9a9a` | `phonon/phonon-31m-en/pocket_state.json` | 2,918 | 307 to 200 | false |
| FluidInference/pocket-tts-coreml | `c2e998e269e4d4b51f74061faddb8450cd8f9a9a` | `phonon/phonon-31m-en/pocket_state.mlmodelc/analytics/coremldata.bin` | 243 | 302 to 200 | false |
| FluidInference/pocket-tts-coreml | `c2e998e269e4d4b51f74061faddb8450cd8f9a9a` | `phonon/phonon-31m-en/pocket_state.mlmodelc/coremldata.bin` | 4,771 | 302 to 200 | false |
| FluidInference/pocket-tts-coreml | `c2e998e269e4d4b51f74061faddb8450cd8f9a9a` | `phonon/phonon-31m-en/pocket_state.mlmodelc/model.mil` | 744,313 | 307 to 200 | false |
| FluidInference/pocket-tts-coreml | `c2e998e269e4d4b51f74061faddb8450cd8f9a9a` | `phonon/phonon-31m-en/pocket_state.mlmodelc/weights/weight.bin` | 79,068,864 | 302 to 200 | false |
| FluidInference/pocket-tts-coreml | `c2e998e269e4d4b51f74061faddb8450cd8f9a9a` | `phonon/phonon-31m-en/voices/characters_Azgorath.safetensors` | 256,208 | 302 to 200 | false |
| FluidInference/pocket-tts-coreml | `c2e998e269e4d4b51f74061faddb8450cd8f9a9a` | `phonon/phonon-31m-en/voices/characters_Captain Silas Blackwater.safetensors` | 256,208 | 302 to 200 | false |
| FluidInference/pocket-tts-coreml | `c2e998e269e4d4b51f74061faddb8450cd8f9a9a` | `phonon/phonon-31m-en/voices/characters_Fairy Godmother.safetensors` | 256,208 | 302 to 200 | false |
| FluidInference/pocket-tts-coreml | `c2e998e269e4d4b51f74061faddb8450cd8f9a9a` | `phonon/phonon-31m-en/voices/characters_Fennick the Young Elf.safetensors` | 256,208 | 302 to 200 | false |
| FluidInference/pocket-tts-coreml | `c2e998e269e4d4b51f74061faddb8450cd8f9a9a` | `phonon/phonon-31m-en/voices/characters_Grub the Ogre.safetensors` | 256,208 | 302 to 200 | false |
| FluidInference/pocket-tts-coreml | `c2e998e269e4d4b51f74061faddb8450cd8f9a9a` | `phonon/phonon-31m-en/voices/characters_Master Aldric.safetensors` | 256,208 | 302 to 200 | false |
| FluidInference/pocket-tts-coreml | `c2e998e269e4d4b51f74061faddb8450cd8f9a9a` | `phonon/phonon-31m-en/voices/characters_Morwenna of the Deep.safetensors` | 256,208 | 302 to 200 | false |
| FluidInference/pocket-tts-coreml | `c2e998e269e4d4b51f74061faddb8450cd8f9a9a` | `phonon/phonon-31m-en/voices/characters_Old Mortlake.safetensors` | 256,208 | 302 to 200 | false |
| FluidInference/pocket-tts-coreml | `c2e998e269e4d4b51f74061faddb8450cd8f9a9a` | `phonon/phonon-31m-en/voices/characters_Sir Bramwell.safetensors` | 256,208 | 302 to 200 | false |
| FluidInference/pocket-tts-coreml | `c2e998e269e4d4b51f74061faddb8450cd8f9a9a` | `phonon/phonon-31m-en/voices/characters_The Snow Queen.safetensors` | 256,208 | 302 to 200 | false |
| FluidInference/pocket-tts-coreml | `c2e998e269e4d4b51f74061faddb8450cd8f9a9a` | `phonon/phonon-31m-en/voices/characters_The Thing Under the Stairs.safetensors` | 256,208 | 302 to 200 | false |
| FluidInference/pocket-tts-coreml | `c2e998e269e4d4b51f74061faddb8450cd8f9a9a` | `phonon/phonon-31m-en/voices/characters_Vyrmathrax the Ancient.safetensors` | 256,208 | 302 to 200 | false |
| FluidInference/pocket-tts-coreml | `c2e998e269e4d4b51f74061faddb8450cd8f9a9a` | `phonon/phonon-31m-en/voices/de_de_Annika.safetensors` | 256,208 | 302 to 200 | false |
| FluidInference/pocket-tts-coreml | `c2e998e269e4d4b51f74061faddb8450cd8f9a9a` | `phonon/phonon-31m-en/voices/de_de_Anton.safetensors` | 256,208 | 302 to 200 | false |
| FluidInference/pocket-tts-coreml | `c2e998e269e4d4b51f74061faddb8450cd8f9a9a` | `phonon/phonon-31m-en/voices/de_de_Henrik.safetensors` | 256,208 | 302 to 200 | false |
| FluidInference/pocket-tts-coreml | `c2e998e269e4d4b51f74061faddb8450cd8f9a9a` | `phonon/phonon-31m-en/voices/de_de_Jette.safetensors` | 256,208 | 302 to 200 | false |
| FluidInference/pocket-tts-coreml | `c2e998e269e4d4b51f74061faddb8450cd8f9a9a` | `phonon/phonon-31m-en/voices/de_de_Josefine.safetensors` | 256,208 | 302 to 200 | false |
| FluidInference/pocket-tts-coreml | `c2e998e269e4d4b51f74061faddb8450cd8f9a9a` | `phonon/phonon-31m-en/voices/de_de_Jule.safetensors` | 256,208 | 302 to 200 | false |
| FluidInference/pocket-tts-coreml | `c2e998e269e4d4b51f74061faddb8450cd8f9a9a` | `phonon/phonon-31m-en/voices/de_de_Leon.safetensors` | 256,208 | 302 to 200 | false |
| FluidInference/pocket-tts-coreml | `c2e998e269e4d4b51f74061faddb8450cd8f9a9a` | `phonon/phonon-31m-en/voices/de_de_Lotta.safetensors` | 256,208 | 302 to 200 | false |
| FluidInference/pocket-tts-coreml | `c2e998e269e4d4b51f74061faddb8450cd8f9a9a` | `phonon/phonon-31m-en/voices/de_de_Mats.safetensors` | 256,208 | 302 to 200 | false |
| FluidInference/pocket-tts-coreml | `c2e998e269e4d4b51f74061faddb8450cd8f9a9a` | `phonon/phonon-31m-en/voices/de_de_Mika.safetensors` | 256,208 | 302 to 200 | false |
| FluidInference/pocket-tts-coreml | `c2e998e269e4d4b51f74061faddb8450cd8f9a9a` | `phonon/phonon-31m-en/voices/de_de_Mirko.safetensors` | 256,208 | 302 to 200 | false |
| FluidInference/pocket-tts-coreml | `c2e998e269e4d4b51f74061faddb8450cd8f9a9a` | `phonon/phonon-31m-en/voices/de_de_Nele.safetensors` | 256,208 | 302 to 200 | false |
| FluidInference/pocket-tts-coreml | `c2e998e269e4d4b51f74061faddb8450cd8f9a9a` | `phonon/phonon-31m-en/voices/de_de_Svea.safetensors` | 256,208 | 302 to 200 | false |
| FluidInference/pocket-tts-coreml | `c2e998e269e4d4b51f74061faddb8450cd8f9a9a` | `phonon/phonon-31m-en/voices/default.safetensors` | 256,208 | 302 to 200 | false |
| FluidInference/pocket-tts-coreml | `c2e998e269e4d4b51f74061faddb8450cd8f9a9a` | `phonon/phonon-31m-en/voices/en_gb_Alfie.safetensors` | 256,208 | 302 to 200 | false |
| FluidInference/pocket-tts-coreml | `c2e998e269e4d4b51f74061faddb8450cd8f9a9a` | `phonon/phonon-31m-en/voices/en_gb_Bryony.safetensors` | 256,208 | 302 to 200 | false |
| FluidInference/pocket-tts-coreml | `c2e998e269e4d4b51f74061faddb8450cd8f9a9a` | `phonon/phonon-31m-en/voices/en_gb_Edwin.safetensors` | 256,208 | 302 to 200 | false |
| FluidInference/pocket-tts-coreml | `c2e998e269e4d4b51f74061faddb8450cd8f9a9a` | `phonon/phonon-31m-en/voices/en_gb_Elodie-Rose.safetensors` | 256,208 | 302 to 200 | false |
| FluidInference/pocket-tts-coreml | `c2e998e269e4d4b51f74061faddb8450cd8f9a9a` | `phonon/phonon-31m-en/voices/en_gb_Freya.safetensors` | 256,208 | 302 to 200 | false |
| FluidInference/pocket-tts-coreml | `c2e998e269e4d4b51f74061faddb8450cd8f9a9a` | `phonon/phonon-31m-en/voices/en_gb_Hugh.safetensors` | 256,208 | 302 to 200 | false |
| FluidInference/pocket-tts-coreml | `c2e998e269e4d4b51f74061faddb8450cd8f9a9a` | `phonon/phonon-31m-en/voices/en_gb_Imogen.safetensors` | 256,208 | 302 to 200 | false |
| FluidInference/pocket-tts-coreml | `c2e998e269e4d4b51f74061faddb8450cd8f9a9a` | `phonon/phonon-31m-en/voices/en_gb_Lottie.safetensors` | 256,208 | 302 to 200 | false |
| FluidInference/pocket-tts-coreml | `c2e998e269e4d4b51f74061faddb8450cd8f9a9a` | `phonon/phonon-31m-en/voices/en_gb_Maeve.safetensors` | 256,208 | 302 to 200 | false |
| FluidInference/pocket-tts-coreml | `c2e998e269e4d4b51f74061faddb8450cd8f9a9a` | `phonon/phonon-31m-en/voices/en_gb_Pippa.safetensors` | 256,208 | 302 to 200 | false |
| FluidInference/pocket-tts-coreml | `c2e998e269e4d4b51f74061faddb8450cd8f9a9a` | `phonon/phonon-31m-en/voices/en_gb_Spencer.safetensors` | 256,208 | 302 to 200 | false |
| FluidInference/pocket-tts-coreml | `c2e998e269e4d4b51f74061faddb8450cd8f9a9a` | `phonon/phonon-31m-en/voices/en_gb_Teddy.safetensors` | 256,208 | 302 to 200 | false |
| FluidInference/pocket-tts-coreml | `c2e998e269e4d4b51f74061faddb8450cd8f9a9a` | `phonon/phonon-31m-en/voices/en_gb_Toby.safetensors` | 256,208 | 302 to 200 | false |
| FluidInference/pocket-tts-coreml | `c2e998e269e4d4b51f74061faddb8450cd8f9a9a` | `phonon/phonon-31m-en/voices/en_us_Delia.safetensors` | 256,208 | 302 to 200 | false |
| FluidInference/pocket-tts-coreml | `c2e998e269e4d4b51f74061faddb8450cd8f9a9a` | `phonon/phonon-31m-en/voices/en_us_Easton.safetensors` | 256,208 | 302 to 200 | false |
| FluidInference/pocket-tts-coreml | `c2e998e269e4d4b51f74061faddb8450cd8f9a9a` | `phonon/phonon-31m-en/voices/en_us_Everly.safetensors` | 256,208 | 302 to 200 | false |
| FluidInference/pocket-tts-coreml | `c2e998e269e4d4b51f74061faddb8450cd8f9a9a` | `phonon/phonon-31m-en/voices/en_us_Hadley.safetensors` | 256,208 | 302 to 200 | false |
| FluidInference/pocket-tts-coreml | `c2e998e269e4d4b51f74061faddb8450cd8f9a9a` | `phonon/phonon-31m-en/voices/en_us_Harper.safetensors` | 256,208 | 302 to 200 | false |
| FluidInference/pocket-tts-coreml | `c2e998e269e4d4b51f74061faddb8450cd8f9a9a` | `phonon/phonon-31m-en/voices/en_us_Juniper.safetensors` | 256,208 | 302 to 200 | false |
| FluidInference/pocket-tts-coreml | `c2e998e269e4d4b51f74061faddb8450cd8f9a9a` | `phonon/phonon-31m-en/voices/en_us_Micah.safetensors` | 256,208 | 302 to 200 | false |
| FluidInference/pocket-tts-coreml | `c2e998e269e4d4b51f74061faddb8450cd8f9a9a` | `phonon/phonon-31m-en/voices/en_us_Miles.safetensors` | 256,208 | 302 to 200 | false |
| FluidInference/pocket-tts-coreml | `c2e998e269e4d4b51f74061faddb8450cd8f9a9a` | `phonon/phonon-31m-en/voices/en_us_Preston.safetensors` | 256,208 | 302 to 200 | false |
| FluidInference/pocket-tts-coreml | `c2e998e269e4d4b51f74061faddb8450cd8f9a9a` | `phonon/phonon-31m-en/voices/en_us_Russell.safetensors` | 256,208 | 302 to 200 | false |
| FluidInference/pocket-tts-coreml | `c2e998e269e4d4b51f74061faddb8450cd8f9a9a` | `phonon/phonon-31m-en/voices/en_us_Trevor.safetensors` | 256,208 | 302 to 200 | false |
| FluidInference/pocket-tts-coreml | `c2e998e269e4d4b51f74061faddb8450cd8f9a9a` | `phonon/phonon-31m-en/voices/en_us_Zoey.safetensors` | 256,208 | 302 to 200 | false |
| FluidInference/pocket-tts-coreml | `c2e998e269e4d4b51f74061faddb8450cd8f9a9a` | `phonon/phonon-31m-en/voices/es_es_Alba.safetensors` | 256,208 | 302 to 200 | false |
| FluidInference/pocket-tts-coreml | `c2e998e269e4d4b51f74061faddb8450cd8f9a9a` | `phonon/phonon-31m-en/voices/es_es_Alicia.safetensors` | 256,208 | 302 to 200 | false |
| FluidInference/pocket-tts-coreml | `c2e998e269e4d4b51f74061faddb8450cd8f9a9a` | `phonon/phonon-31m-en/voices/es_es_Borja.safetensors` | 256,208 | 302 to 200 | false |
| FluidInference/pocket-tts-coreml | `c2e998e269e4d4b51f74061faddb8450cd8f9a9a` | `phonon/phonon-31m-en/voices/es_es_Candela.safetensors` | 256,208 | 302 to 200 | false |
| FluidInference/pocket-tts-coreml | `c2e998e269e4d4b51f74061faddb8450cd8f9a9a` | `phonon/phonon-31m-en/voices/es_es_Celia.safetensors` | 256,208 | 302 to 200 | false |
| FluidInference/pocket-tts-coreml | `c2e998e269e4d4b51f74061faddb8450cd8f9a9a` | `phonon/phonon-31m-en/voices/es_es_Guillermo.safetensors` | 256,208 | 302 to 200 | false |
| FluidInference/pocket-tts-coreml | `c2e998e269e4d4b51f74061faddb8450cd8f9a9a` | `phonon/phonon-31m-en/voices/es_es_Iker.safetensors` | 256,208 | 302 to 200 | false |
| FluidInference/pocket-tts-coreml | `c2e998e269e4d4b51f74061faddb8450cd8f9a9a` | `phonon/phonon-31m-en/voices/es_es_Izan.safetensors` | 256,208 | 302 to 200 | false |
| FluidInference/pocket-tts-coreml | `c2e998e269e4d4b51f74061faddb8450cd8f9a9a` | `phonon/phonon-31m-en/voices/es_es_Lucía-Sol.safetensors` | 256,208 | 302 to 200 | false |
| FluidInference/pocket-tts-coreml | `c2e998e269e4d4b51f74061faddb8450cd8f9a9a` | `phonon/phonon-31m-en/voices/es_es_Marcos.safetensors` | 256,208 | 302 to 200 | false |
| FluidInference/pocket-tts-coreml | `c2e998e269e4d4b51f74061faddb8450cd8f9a9a` | `phonon/phonon-31m-en/voices/es_es_Nerea.safetensors` | 256,208 | 302 to 200 | false |
| FluidInference/pocket-tts-coreml | `c2e998e269e4d4b51f74061faddb8450cd8f9a9a` | `phonon/phonon-31m-en/voices/es_es_Noa.safetensors` | 256,208 | 302 to 200 | false |
| FluidInference/pocket-tts-coreml | `c2e998e269e4d4b51f74061faddb8450cd8f9a9a` | `phonon/phonon-31m-en/voices/es_es_Nuno.safetensors` | 256,208 | 302 to 200 | false |
| FluidInference/pocket-tts-coreml | `c2e998e269e4d4b51f74061faddb8450cd8f9a9a` | `phonon/phonon-31m-en/voices/es_mx_Aldo.safetensors` | 256,208 | 302 to 200 | false |
| FluidInference/pocket-tts-coreml | `c2e998e269e4d4b51f74061faddb8450cd8f9a9a` | `phonon/phonon-31m-en/voices/es_mx_Azucena.safetensors` | 256,208 | 302 to 200 | false |
| FluidInference/pocket-tts-coreml | `c2e998e269e4d4b51f74061faddb8450cd8f9a9a` | `phonon/phonon-31m-en/voices/es_mx_Danae.safetensors` | 256,208 | 302 to 200 | false |
| FluidInference/pocket-tts-coreml | `c2e998e269e4d4b51f74061faddb8450cd8f9a9a` | `phonon/phonon-31m-en/voices/es_mx_Jimena.safetensors` | 256,208 | 302 to 200 | false |
| FluidInference/pocket-tts-coreml | `c2e998e269e4d4b51f74061faddb8450cd8f9a9a` | `phonon/phonon-31m-en/voices/es_mx_Lalo.safetensors` | 256,208 | 302 to 200 | false |
| FluidInference/pocket-tts-coreml | `c2e998e269e4d4b51f74061faddb8450cd8f9a9a` | `phonon/phonon-31m-en/voices/es_mx_Leonel.safetensors` | 256,208 | 302 to 200 | false |
| FluidInference/pocket-tts-coreml | `c2e998e269e4d4b51f74061faddb8450cd8f9a9a` | `phonon/phonon-31m-en/voices/es_mx_Nico.safetensors` | 256,208 | 302 to 200 | false |
| FluidInference/pocket-tts-coreml | `c2e998e269e4d4b51f74061faddb8450cd8f9a9a` | `phonon/phonon-31m-en/voices/es_mx_Rebeca.safetensors` | 256,208 | 302 to 200 | false |
| FluidInference/pocket-tts-coreml | `c2e998e269e4d4b51f74061faddb8450cd8f9a9a` | `phonon/phonon-31m-en/voices/es_mx_Regina.safetensors` | 256,208 | 302 to 200 | false |
| FluidInference/pocket-tts-coreml | `c2e998e269e4d4b51f74061faddb8450cd8f9a9a` | `phonon/phonon-31m-en/voices/es_mx_Ulises.safetensors` | 256,208 | 302 to 200 | false |
| FluidInference/pocket-tts-coreml | `c2e998e269e4d4b51f74061faddb8450cd8f9a9a` | `phonon/phonon-31m-en/voices/es_mx_Ximena.safetensors` | 256,208 | 302 to 200 | false |
| FluidInference/pocket-tts-coreml | `c2e998e269e4d4b51f74061faddb8450cd8f9a9a` | `phonon/phonon-31m-en/voices/es_mx_Yael.safetensors` | 256,208 | 302 to 200 | false |
| FluidInference/pocket-tts-coreml | `c2e998e269e4d4b51f74061faddb8450cd8f9a9a` | `phonon/phonon-31m-en/voices/fr_fr_Arnaud.safetensors` | 256,208 | 302 to 200 | false |
| FluidInference/pocket-tts-coreml | `c2e998e269e4d4b51f74061faddb8450cd8f9a9a` | `phonon/phonon-31m-en/voices/fr_fr_Augustin.safetensors` | 256,208 | 302 to 200 | false |
| FluidInference/pocket-tts-coreml | `c2e998e269e4d4b51f74061faddb8450cd8f9a9a` | `phonon/phonon-31m-en/voices/fr_fr_Baptiste.safetensors` | 256,208 | 302 to 200 | false |
| FluidInference/pocket-tts-coreml | `c2e998e269e4d4b51f74061faddb8450cd8f9a9a` | `phonon/phonon-31m-en/voices/fr_fr_Coralie.safetensors` | 256,208 | 302 to 200 | false |
| FluidInference/pocket-tts-coreml | `c2e998e269e4d4b51f74061faddb8450cd8f9a9a` | `phonon/phonon-31m-en/voices/fr_fr_Hélène.safetensors` | 256,208 | 302 to 200 | false |
| FluidInference/pocket-tts-coreml | `c2e998e269e4d4b51f74061faddb8450cd8f9a9a` | `phonon/phonon-31m-en/voices/fr_fr_Jules.safetensors` | 256,208 | 302 to 200 | false |
| FluidInference/pocket-tts-coreml | `c2e998e269e4d4b51f74061faddb8450cd8f9a9a` | `phonon/phonon-31m-en/voices/fr_fr_Lilou.safetensors` | 256,208 | 302 to 200 | false |
| FluidInference/pocket-tts-coreml | `c2e998e269e4d4b51f74061faddb8450cd8f9a9a` | `phonon/phonon-31m-en/voices/fr_fr_Lucien.safetensors` | 256,208 | 302 to 200 | false |
| FluidInference/pocket-tts-coreml | `c2e998e269e4d4b51f74061faddb8450cd8f9a9a` | `phonon/phonon-31m-en/voices/fr_fr_Marius.safetensors` | 256,208 | 302 to 200 | false |
| FluidInference/pocket-tts-coreml | `c2e998e269e4d4b51f74061faddb8450cd8f9a9a` | `phonon/phonon-31m-en/voices/fr_fr_Maëlys.safetensors` | 256,208 | 302 to 200 | false |
| FluidInference/pocket-tts-coreml | `c2e998e269e4d4b51f74061faddb8450cd8f9a9a` | `phonon/phonon-31m-en/voices/fr_fr_Ninon.safetensors` | 256,208 | 302 to 200 | false |
| FluidInference/pocket-tts-coreml | `c2e998e269e4d4b51f74061faddb8450cd8f9a9a` | `phonon/phonon-31m-en/voices/fr_fr_Noémie.safetensors` | 256,208 | 302 to 200 | false |
| FluidInference/pocket-tts-coreml | `c2e998e269e4d4b51f74061faddb8450cd8f9a9a` | `phonon/phonon-31m-en/voices/fr_fr_Orianne.safetensors` | 256,208 | 302 to 200 | false |
| FluidInference/pocket-tts-coreml | `c2e998e269e4d4b51f74061faddb8450cd8f9a9a` | `phonon/phonon-31m-en/voices/fr_fr_Solène.safetensors` | 256,208 | 302 to 200 | false |
| FluidInference/pocket-tts-coreml | `c2e998e269e4d4b51f74061faddb8450cd8f9a9a` | `phonon/phonon-31m-en/voices/fr_fr_Tiphaine.safetensors` | 256,208 | 302 to 200 | false |
| FluidInference/pocket-tts-coreml | `c2e998e269e4d4b51f74061faddb8450cd8f9a9a` | `phonon/phonon-31m-en/voices/pt_br_Clarice.safetensors` | 256,208 | 302 to 200 | false |
| FluidInference/pocket-tts-coreml | `c2e998e269e4d4b51f74061faddb8450cd8f9a9a` | `phonon/phonon-31m-en/voices/pt_br_Davi.safetensors` | 256,208 | 302 to 200 | false |
| FluidInference/pocket-tts-coreml | `c2e998e269e4d4b51f74061faddb8450cd8f9a9a` | `phonon/phonon-31m-en/voices/pt_br_Débora.safetensors` | 256,208 | 302 to 200 | false |
| FluidInference/pocket-tts-coreml | `c2e998e269e4d4b51f74061faddb8450cd8f9a9a` | `phonon/phonon-31m-en/voices/pt_br_Flávia.safetensors` | 256,208 | 302 to 200 | false |
| FluidInference/pocket-tts-coreml | `c2e998e269e4d4b51f74061faddb8450cd8f9a9a` | `phonon/phonon-31m-en/voices/pt_br_Igor.safetensors` | 256,208 | 302 to 200 | false |
| FluidInference/pocket-tts-coreml | `c2e998e269e4d4b51f74061faddb8450cd8f9a9a` | `phonon/phonon-31m-en/voices/pt_br_Isadora.safetensors` | 256,208 | 302 to 200 | false |
| FluidInference/pocket-tts-coreml | `c2e998e269e4d4b51f74061faddb8450cd8f9a9a` | `phonon/phonon-31m-en/voices/pt_br_Kauã.safetensors` | 256,208 | 302 to 200 | false |
| FluidInference/pocket-tts-coreml | `c2e998e269e4d4b51f74061faddb8450cd8f9a9a` | `phonon/phonon-31m-en/voices/pt_br_Manuela-Lu.safetensors` | 256,208 | 302 to 200 | false |
| FluidInference/pocket-tts-coreml | `c2e998e269e4d4b51f74061faddb8450cd8f9a9a` | `phonon/phonon-31m-en/voices/pt_br_Marcelo.safetensors` | 256,208 | 302 to 200 | false |
| FluidInference/pocket-tts-coreml | `c2e998e269e4d4b51f74061faddb8450cd8f9a9a` | `phonon/phonon-31m-en/voices/pt_br_Otávio.safetensors` | 256,208 | 302 to 200 | false |
| FluidInference/pocket-tts-coreml | `c2e998e269e4d4b51f74061faddb8450cd8f9a9a` | `phonon/phonon-31m-en/voices/pt_br_Rafaela.safetensors` | 256,208 | 302 to 200 | false |
| FluidInference/pocket-tts-coreml | `c2e998e269e4d4b51f74061faddb8450cd8f9a9a` | `phonon/phonon-31m-en/voices/pt_br_Vitor.safetensors` | 256,208 | 302 to 200 | false |
| FluidInference/pocket-tts-coreml | `c2e998e269e4d4b51f74061faddb8450cd8f9a9a` | `phonon/phonon-31m-en/voices_catalog.jsonl` | 33,407 | 307 to 200 | false |
| FluidInference/pocket-tts-coreml | `c2e998e269e4d4b51f74061faddb8450cd8f9a9a` | `v4/english_18m_2026-10/arch.json` | 643 | 307 to 200 | false |
| FluidInference/pocket-tts-coreml | `c2e998e269e4d4b51f74061faddb8450cd8f9a9a` | `v4/english_18m_2026-10/cond_prefill.mlmodelc/analytics/coremldata.bin` | 243 | 302 to 200 | false |
| FluidInference/pocket-tts-coreml | `c2e998e269e4d4b51f74061faddb8450cd8f9a9a` | `v4/english_18m_2026-10/cond_prefill.mlmodelc/coremldata.bin` | 1,725 | 302 to 200 | false |
| FluidInference/pocket-tts-coreml | `c2e998e269e4d4b51f74061faddb8450cd8f9a9a` | `v4/english_18m_2026-10/cond_prefill.mlmodelc/model.mil` | 149,272 | 307 to 200 | false |
| FluidInference/pocket-tts-coreml | `c2e998e269e4d4b51f74061faddb8450cd8f9a9a` | `v4/english_18m_2026-10/cond_prefill.mlmodelc/weights/weight.bin` | 47,395,392 | 302 to 200 | false |
| FluidInference/pocket-tts-coreml | `c2e998e269e4d4b51f74061faddb8450cd8f9a9a` | `v4/english_18m_2026-10/constants/arch.json` | 643 | 307 to 200 | false |
| FluidInference/pocket-tts-coreml | `c2e998e269e4d4b51f74061faddb8450cd8f9a9a` | `v4/english_18m_2026-10/constants/bos_before_voice.npy` | 2,176 | 302 to 200 | false |
| FluidInference/pocket-tts-coreml | `c2e998e269e4d4b51f74061faddb8450cd8f9a9a` | `v4/english_18m_2026-10/constants/bos_emb.npy` | 256 | 302 to 200 | false |
| FluidInference/pocket-tts-coreml | `c2e998e269e4d4b51f74061faddb8450cd8f9a9a` | `v4/english_18m_2026-10/constants/emb_mean.npy` | 256 | 302 to 200 | false |
| FluidInference/pocket-tts-coreml | `c2e998e269e4d4b51f74061faddb8450cd8f9a9a` | `v4/english_18m_2026-10/constants/emb_std.npy` | 256 | 302 to 200 | false |
| FluidInference/pocket-tts-coreml | `c2e998e269e4d4b51f74061faddb8450cd8f9a9a` | `v4/english_18m_2026-10/constants/mimi_state_spec.json` | 6,763 | 307 to 200 | false |
| FluidInference/pocket-tts-coreml | `c2e998e269e4d4b51f74061faddb8450cd8f9a9a` | `v4/english_18m_2026-10/constants/speaker_proj_weight.npy` | 65,664 | 302 to 200 | false |
| FluidInference/pocket-tts-coreml | `c2e998e269e4d4b51f74061faddb8450cd8f9a9a` | `v4/english_18m_2026-10/constants/text_embed_table.npy` | 8,194,176 | 302 to 200 | false |
| FluidInference/pocket-tts-coreml | `c2e998e269e4d4b51f74061faddb8450cd8f9a9a` | `v4/english_18m_2026-10/constants_bin/alba.safetensors` | 4,163,992 | 302 to 200 | false |
| FluidInference/pocket-tts-coreml | `c2e998e269e4d4b51f74061faddb8450cd8f9a9a` | `v4/english_18m_2026-10/constants_bin/anna.safetensors` | 5,081,496 | 302 to 200 | false |
| FluidInference/pocket-tts-coreml | `c2e998e269e4d4b51f74061faddb8450cd8f9a9a` | `v4/english_18m_2026-10/constants_bin/azelma.safetensors` | 4,786,584 | 302 to 200 | false |
| FluidInference/pocket-tts-coreml | `c2e998e269e4d4b51f74061faddb8450cd8f9a9a` | `v4/english_18m_2026-10/constants_bin/bill_boerst.safetensors` | 4,360,600 | 302 to 200 | false |
| FluidInference/pocket-tts-coreml | `c2e998e269e4d4b51f74061faddb8450cd8f9a9a` | `v4/english_18m_2026-10/constants_bin/bos_before_voice.bin` | 2,048 | 302 to 200 | false |
| FluidInference/pocket-tts-coreml | `c2e998e269e4d4b51f74061faddb8450cd8f9a9a` | `v4/english_18m_2026-10/constants_bin/bos_emb.bin` | 128 | 302 to 200 | false |
| FluidInference/pocket-tts-coreml | `c2e998e269e4d4b51f74061faddb8450cd8f9a9a` | `v4/english_18m_2026-10/constants_bin/caro_davy.safetensors` | 3,377,560 | 302 to 200 | false |
| FluidInference/pocket-tts-coreml | `c2e998e269e4d4b51f74061faddb8450cd8f9a9a` | `v4/english_18m_2026-10/constants_bin/charles.safetensors` | 4,131,224 | 302 to 200 | false |
| FluidInference/pocket-tts-coreml | `c2e998e269e4d4b51f74061faddb8450cd8f9a9a` | `v4/english_18m_2026-10/constants_bin/cosette.safetensors` | 4,163,992 | 302 to 200 | false |
| FluidInference/pocket-tts-coreml | `c2e998e269e4d4b51f74061faddb8450cd8f9a9a` | `v4/english_18m_2026-10/constants_bin/daan.safetensors` | 2,591,120 | 302 to 200 | false |
| FluidInference/pocket-tts-coreml | `c2e998e269e4d4b51f74061faddb8450cd8f9a9a` | `v4/english_18m_2026-10/constants_bin/eponine.safetensors` | 4,622,744 | 302 to 200 | false |
| FluidInference/pocket-tts-coreml | `c2e998e269e4d4b51f74061faddb8450cd8f9a9a` | `v4/english_18m_2026-10/constants_bin/estelle.safetensors` | 5,048,728 | 302 to 200 | false |
| FluidInference/pocket-tts-coreml | `c2e998e269e4d4b51f74061faddb8450cd8f9a9a` | `v4/english_18m_2026-10/constants_bin/eve.safetensors` | 4,098,456 | 302 to 200 | false |
| FluidInference/pocket-tts-coreml | `c2e998e269e4d4b51f74061faddb8450cd8f9a9a` | `v4/english_18m_2026-10/constants_bin/fantine.safetensors` | 4,295,064 | 302 to 200 | false |
| FluidInference/pocket-tts-coreml | `c2e998e269e4d4b51f74061faddb8450cd8f9a9a` | `v4/english_18m_2026-10/constants_bin/george.safetensors` | 3,934,616 | 302 to 200 | false |
| FluidInference/pocket-tts-coreml | `c2e998e269e4d4b51f74061faddb8450cd8f9a9a` | `v4/english_18m_2026-10/constants_bin/giovanni.safetensors` | 2,754,960 | 302 to 200 | false |
| FluidInference/pocket-tts-coreml | `c2e998e269e4d4b51f74061faddb8450cd8f9a9a` | `v4/english_18m_2026-10/constants_bin/jane.safetensors` | 4,884,888 | 302 to 200 | false |
| FluidInference/pocket-tts-coreml | `c2e998e269e4d4b51f74061faddb8450cd8f9a9a` | `v4/english_18m_2026-10/constants_bin/javert.safetensors` | 4,098,456 | 302 to 200 | false |
| FluidInference/pocket-tts-coreml | `c2e998e269e4d4b51f74061faddb8450cd8f9a9a` | `v4/english_18m_2026-10/constants_bin/jean.safetensors` | 4,065,688 | 302 to 200 | false |
| FluidInference/pocket-tts-coreml | `c2e998e269e4d4b51f74061faddb8450cd8f9a9a` | `v4/english_18m_2026-10/constants_bin/juergen.safetensors` | 4,065,688 | 302 to 200 | false |
| FluidInference/pocket-tts-coreml | `c2e998e269e4d4b51f74061faddb8450cd8f9a9a` | `v4/english_18m_2026-10/constants_bin/lola.safetensors` | 3,639,704 | 302 to 200 | false |
| FluidInference/pocket-tts-coreml | `c2e998e269e4d4b51f74061faddb8450cd8f9a9a` | `v4/english_18m_2026-10/constants_bin/marius.safetensors` | 4,163,992 | 302 to 200 | false |
| FluidInference/pocket-tts-coreml | `c2e998e269e4d4b51f74061faddb8450cd8f9a9a` | `v4/english_18m_2026-10/constants_bin/mary.safetensors` | 3,967,384 | 302 to 200 | false |
| FluidInference/pocket-tts-coreml | `c2e998e269e4d4b51f74061faddb8450cd8f9a9a` | `v4/english_18m_2026-10/constants_bin/michael.safetensors` | 4,753,816 | 302 to 200 | false |
| FluidInference/pocket-tts-coreml | `c2e998e269e4d4b51f74061faddb8450cd8f9a9a` | `v4/english_18m_2026-10/constants_bin/paul.safetensors` | 4,557,208 | 302 to 200 | false |
| FluidInference/pocket-tts-coreml | `c2e998e269e4d4b51f74061faddb8450cd8f9a9a` | `v4/english_18m_2026-10/constants_bin/peter_yearsley.safetensors` | 2,427,280 | 302 to 200 | false |
| FluidInference/pocket-tts-coreml | `c2e998e269e4d4b51f74061faddb8450cd8f9a9a` | `v4/english_18m_2026-10/constants_bin/rafael.safetensors` | 4,163,992 | 302 to 200 | false |
| FluidInference/pocket-tts-coreml | `c2e998e269e4d4b51f74061faddb8450cd8f9a9a` | `v4/english_18m_2026-10/constants_bin/speaker_proj_weight.bin` | 65,536 | 302 to 200 | false |
| FluidInference/pocket-tts-coreml | `c2e998e269e4d4b51f74061faddb8450cd8f9a9a` | `v4/english_18m_2026-10/constants_bin/stuart_bell.safetensors` | 3,377,560 | 302 to 200 | false |
| FluidInference/pocket-tts-coreml | `c2e998e269e4d4b51f74061faddb8450cd8f9a9a` | `v4/english_18m_2026-10/constants_bin/text_embed_table.bin` | 8,194,048 | 302 to 200 | false |
| FluidInference/pocket-tts-coreml | `c2e998e269e4d4b51f74061faddb8450cd8f9a9a` | `v4/english_18m_2026-10/constants_bin/tokenizer.model` | 59,339 | 302 to 200 | false |
| FluidInference/pocket-tts-coreml | `c2e998e269e4d4b51f74061faddb8450cd8f9a9a` | `v4/english_18m_2026-10/constants_bin/vera.safetensors` | 4,458,904 | 302 to 200 | false |
| FluidInference/pocket-tts-coreml | `c2e998e269e4d4b51f74061faddb8450cd8f9a9a` | `v4/english_18m_2026-10/flow_decoder_fused.mlmodelc/analytics/coremldata.bin` | 243 | 302 to 200 | false |
| FluidInference/pocket-tts-coreml | `c2e998e269e4d4b51f74061faddb8450cd8f9a9a` | `v4/english_18m_2026-10/flow_decoder_fused.mlmodelc/coremldata.bin` | 412 | 302 to 200 | false |
| FluidInference/pocket-tts-coreml | `c2e998e269e4d4b51f74061faddb8450cd8f9a9a` | `v4/english_18m_2026-10/flow_decoder_fused.mlmodelc/model.mil` | 30,152 | 307 to 200 | false |
| FluidInference/pocket-tts-coreml | `c2e998e269e4d4b51f74061faddb8450cd8f9a9a` | `v4/english_18m_2026-10/flow_decoder_fused.mlmodelc/weights/weight.bin` | 3,369,664 | 302 to 200 | false |
| FluidInference/pocket-tts-coreml | `c2e998e269e4d4b51f74061faddb8450cd8f9a9a` | `v4/english_18m_2026-10/flowlm_step.mlmodelc/analytics/coremldata.bin` | 243 | 302 to 200 | false |
| FluidInference/pocket-tts-coreml | `c2e998e269e4d4b51f74061faddb8450cd8f9a9a` | `v4/english_18m_2026-10/flowlm_step.mlmodelc/coremldata.bin` | 1,750 | 302 to 200 | false |
| FluidInference/pocket-tts-coreml | `c2e998e269e4d4b51f74061faddb8450cd8f9a9a` | `v4/english_18m_2026-10/flowlm_step.mlmodelc/model.mil` | 171,535 | 307 to 200 | false |
| FluidInference/pocket-tts-coreml | `c2e998e269e4d4b51f74061faddb8450cd8f9a9a` | `v4/english_18m_2026-10/flowlm_step.mlmodelc/weights/weight.bin` | 50,747,648 | 302 to 200 | false |
| FluidInference/pocket-tts-coreml | `c2e998e269e4d4b51f74061faddb8450cd8f9a9a` | `v4/english_18m_2026-10/manifest.json` | 9,096 | 307 to 200 | false |
| FluidInference/pocket-tts-coreml | `c2e998e269e4d4b51f74061faddb8450cd8f9a9a` | `v4/english_18m_2026-10/mimi_decoder.mlmodelc/analytics/coremldata.bin` | 243 | 302 to 200 | false |
| FluidInference/pocket-tts-coreml | `c2e998e269e4d4b51f74061faddb8450cd8f9a9a` | `v4/english_18m_2026-10/mimi_decoder.mlmodelc/coremldata.bin` | 4,968 | 302 to 200 | false |
| FluidInference/pocket-tts-coreml | `c2e998e269e4d4b51f74061faddb8450cd8f9a9a` | `v4/english_18m_2026-10/mimi_decoder.mlmodelc/model.mil` | 306,570 | 307 to 200 | false |
| FluidInference/pocket-tts-coreml | `c2e998e269e4d4b51f74061faddb8450cd8f9a9a` | `v4/english_18m_2026-10/mimi_decoder.mlmodelc/weights/weight.bin` | 10,701,824 | 302 to 200 | false |
| FluidInference/pocket-tts-coreml | `c2e998e269e4d4b51f74061faddb8450cd8f9a9a` | `v4/english_18m_2026-10/pocket_state.json` | 5,467 | 307 to 200 | false |
| FluidInference/pocket-tts-coreml | `c2e998e269e4d4b51f74061faddb8450cd8f9a9a` | `v4/english_18m_2026-10/pocket_state.mlmodelc/analytics/coremldata.bin` | 243 | 302 to 200 | false |
| FluidInference/pocket-tts-coreml | `c2e998e269e4d4b51f74061faddb8450cd8f9a9a` | `v4/english_18m_2026-10/pocket_state.mlmodelc/coremldata.bin` | 8,562 | 302 to 200 | false |
| FluidInference/pocket-tts-coreml | `c2e998e269e4d4b51f74061faddb8450cd8f9a9a` | `v4/english_18m_2026-10/pocket_state.mlmodelc/model.mil` | 1,356,621 | 307 to 200 | false |
| FluidInference/pocket-tts-coreml | `c2e998e269e4d4b51f74061faddb8450cd8f9a9a` | `v4/english_18m_2026-10/pocket_state.mlmodelc/weights/weight.bin` | 32,419,200 | 302 to 200 | false |
| FluidInference/pocket-tts-coreml | `c2e998e269e4d4b51f74061faddb8450cd8f9a9a` | `v4/english_2m_2026-10/arch.json` | 638 | 307 to 200 | false |
| FluidInference/pocket-tts-coreml | `c2e998e269e4d4b51f74061faddb8450cd8f9a9a` | `v4/english_2m_2026-10/cond_prefill.mlmodelc/analytics/coremldata.bin` | 243 | 302 to 200 | false |
| FluidInference/pocket-tts-coreml | `c2e998e269e4d4b51f74061faddb8450cd8f9a9a` | `v4/english_2m_2026-10/cond_prefill.mlmodelc/coremldata.bin` | 1,724 | 302 to 200 | false |
| FluidInference/pocket-tts-coreml | `c2e998e269e4d4b51f74061faddb8450cd8f9a9a` | `v4/english_2m_2026-10/cond_prefill.mlmodelc/model.mil` | 153,852 | 307 to 200 | false |
| FluidInference/pocket-tts-coreml | `c2e998e269e4d4b51f74061faddb8450cd8f9a9a` | `v4/english_2m_2026-10/cond_prefill.mlmodelc/weights/weight.bin` | 2,509,248 | 302 to 200 | false |
| FluidInference/pocket-tts-coreml | `c2e998e269e4d4b51f74061faddb8450cd8f9a9a` | `v4/english_2m_2026-10/constants/arch.json` | 638 | 307 to 200 | false |
| FluidInference/pocket-tts-coreml | `c2e998e269e4d4b51f74061faddb8450cd8f9a9a` | `v4/english_2m_2026-10/constants/bos_before_voice.npy` | 576 | 302 to 200 | false |
| FluidInference/pocket-tts-coreml | `c2e998e269e4d4b51f74061faddb8450cd8f9a9a` | `v4/english_2m_2026-10/constants/bos_emb.npy` | 256 | 302 to 200 | false |
| FluidInference/pocket-tts-coreml | `c2e998e269e4d4b51f74061faddb8450cd8f9a9a` | `v4/english_2m_2026-10/constants/emb_mean.npy` | 256 | 302 to 200 | false |
| FluidInference/pocket-tts-coreml | `c2e998e269e4d4b51f74061faddb8450cd8f9a9a` | `v4/english_2m_2026-10/constants/emb_std.npy` | 256 | 302 to 200 | false |
| FluidInference/pocket-tts-coreml | `c2e998e269e4d4b51f74061faddb8450cd8f9a9a` | `v4/english_2m_2026-10/constants/mimi_state_spec.json` | 3,265 | 307 to 200 | false |
| FluidInference/pocket-tts-coreml | `c2e998e269e4d4b51f74061faddb8450cd8f9a9a` | `v4/english_2m_2026-10/constants/speaker_proj_weight.npy` | 14,464 | 302 to 200 | false |
| FluidInference/pocket-tts-coreml | `c2e998e269e4d4b51f74061faddb8450cd8f9a9a` | `v4/english_2m_2026-10/constants/text_embed_table.npy` | 1,792,576 | 302 to 200 | false |
| FluidInference/pocket-tts-coreml | `c2e998e269e4d4b51f74061faddb8450cd8f9a9a` | `v4/english_2m_2026-10/constants_bin/alba.safetensors` | 522,632 | 302 to 200 | false |
| FluidInference/pocket-tts-coreml | `c2e998e269e4d4b51f74061faddb8450cd8f9a9a` | `v4/english_2m_2026-10/constants_bin/anna.safetensors` | 637,320 | 302 to 200 | false |
| FluidInference/pocket-tts-coreml | `c2e998e269e4d4b51f74061faddb8450cd8f9a9a` | `v4/english_2m_2026-10/constants_bin/azelma.safetensors` | 600,456 | 302 to 200 | false |
| FluidInference/pocket-tts-coreml | `c2e998e269e4d4b51f74061faddb8450cd8f9a9a` | `v4/english_2m_2026-10/constants_bin/bill_boerst.safetensors` | 547,208 | 302 to 200 | false |
| FluidInference/pocket-tts-coreml | `c2e998e269e4d4b51f74061faddb8450cd8f9a9a` | `v4/english_2m_2026-10/constants_bin/bos_before_voice.bin` | 448 | 302 to 200 | false |
| FluidInference/pocket-tts-coreml | `c2e998e269e4d4b51f74061faddb8450cd8f9a9a` | `v4/english_2m_2026-10/constants_bin/bos_emb.bin` | 128 | 302 to 200 | false |
| FluidInference/pocket-tts-coreml | `c2e998e269e4d4b51f74061faddb8450cd8f9a9a` | `v4/english_2m_2026-10/constants_bin/caro_davy.safetensors` | 424,328 | 302 to 200 | false |
| FluidInference/pocket-tts-coreml | `c2e998e269e4d4b51f74061faddb8450cd8f9a9a` | `v4/english_2m_2026-10/constants_bin/charles.safetensors` | 518,536 | 302 to 200 | false |
| FluidInference/pocket-tts-coreml | `c2e998e269e4d4b51f74061faddb8450cd8f9a9a` | `v4/english_2m_2026-10/constants_bin/cosette.safetensors` | 522,632 | 302 to 200 | false |
| FluidInference/pocket-tts-coreml | `c2e998e269e4d4b51f74061faddb8450cd8f9a9a` | `v4/english_2m_2026-10/constants_bin/daan.safetensors` | 326,016 | 302 to 200 | false |
| FluidInference/pocket-tts-coreml | `c2e998e269e4d4b51f74061faddb8450cd8f9a9a` | `v4/english_2m_2026-10/constants_bin/eponine.safetensors` | 579,976 | 302 to 200 | false |
| FluidInference/pocket-tts-coreml | `c2e998e269e4d4b51f74061faddb8450cd8f9a9a` | `v4/english_2m_2026-10/constants_bin/estelle.safetensors` | 633,224 | 302 to 200 | false |
| FluidInference/pocket-tts-coreml | `c2e998e269e4d4b51f74061faddb8450cd8f9a9a` | `v4/english_2m_2026-10/constants_bin/eve.safetensors` | 514,440 | 302 to 200 | false |
| FluidInference/pocket-tts-coreml | `c2e998e269e4d4b51f74061faddb8450cd8f9a9a` | `v4/english_2m_2026-10/constants_bin/fantine.safetensors` | 539,016 | 302 to 200 | false |
| FluidInference/pocket-tts-coreml | `c2e998e269e4d4b51f74061faddb8450cd8f9a9a` | `v4/english_2m_2026-10/constants_bin/george.safetensors` | 493,960 | 302 to 200 | false |
| FluidInference/pocket-tts-coreml | `c2e998e269e4d4b51f74061faddb8450cd8f9a9a` | `v4/english_2m_2026-10/constants_bin/giovanni.safetensors` | 346,496 | 302 to 200 | false |
| FluidInference/pocket-tts-coreml | `c2e998e269e4d4b51f74061faddb8450cd8f9a9a` | `v4/english_2m_2026-10/constants_bin/jane.safetensors` | 612,744 | 302 to 200 | false |
| FluidInference/pocket-tts-coreml | `c2e998e269e4d4b51f74061faddb8450cd8f9a9a` | `v4/english_2m_2026-10/constants_bin/javert.safetensors` | 514,440 | 302 to 200 | false |
| FluidInference/pocket-tts-coreml | `c2e998e269e4d4b51f74061faddb8450cd8f9a9a` | `v4/english_2m_2026-10/constants_bin/jean.safetensors` | 510,344 | 302 to 200 | false |
| FluidInference/pocket-tts-coreml | `c2e998e269e4d4b51f74061faddb8450cd8f9a9a` | `v4/english_2m_2026-10/constants_bin/juergen.safetensors` | 510,344 | 302 to 200 | false |
| FluidInference/pocket-tts-coreml | `c2e998e269e4d4b51f74061faddb8450cd8f9a9a` | `v4/english_2m_2026-10/constants_bin/lola.safetensors` | 457,096 | 302 to 200 | false |
| FluidInference/pocket-tts-coreml | `c2e998e269e4d4b51f74061faddb8450cd8f9a9a` | `v4/english_2m_2026-10/constants_bin/marius.safetensors` | 522,632 | 302 to 200 | false |
| FluidInference/pocket-tts-coreml | `c2e998e269e4d4b51f74061faddb8450cd8f9a9a` | `v4/english_2m_2026-10/constants_bin/mary.safetensors` | 498,056 | 302 to 200 | false |
| FluidInference/pocket-tts-coreml | `c2e998e269e4d4b51f74061faddb8450cd8f9a9a` | `v4/english_2m_2026-10/constants_bin/michael.safetensors` | 596,360 | 302 to 200 | false |
| FluidInference/pocket-tts-coreml | `c2e998e269e4d4b51f74061faddb8450cd8f9a9a` | `v4/english_2m_2026-10/constants_bin/paul.safetensors` | 571,784 | 302 to 200 | false |
| FluidInference/pocket-tts-coreml | `c2e998e269e4d4b51f74061faddb8450cd8f9a9a` | `v4/english_2m_2026-10/constants_bin/peter_yearsley.safetensors` | 305,536 | 302 to 200 | false |
| FluidInference/pocket-tts-coreml | `c2e998e269e4d4b51f74061faddb8450cd8f9a9a` | `v4/english_2m_2026-10/constants_bin/rafael.safetensors` | 522,632 | 302 to 200 | false |
| FluidInference/pocket-tts-coreml | `c2e998e269e4d4b51f74061faddb8450cd8f9a9a` | `v4/english_2m_2026-10/constants_bin/speaker_proj_weight.bin` | 14,336 | 302 to 200 | false |
| FluidInference/pocket-tts-coreml | `c2e998e269e4d4b51f74061faddb8450cd8f9a9a` | `v4/english_2m_2026-10/constants_bin/stuart_bell.safetensors` | 424,328 | 302 to 200 | false |
| FluidInference/pocket-tts-coreml | `c2e998e269e4d4b51f74061faddb8450cd8f9a9a` | `v4/english_2m_2026-10/constants_bin/text_embed_table.bin` | 1,792,448 | 302 to 200 | false |
| FluidInference/pocket-tts-coreml | `c2e998e269e4d4b51f74061faddb8450cd8f9a9a` | `v4/english_2m_2026-10/constants_bin/tokenizer.model` | 59,339 | 302 to 200 | false |
| FluidInference/pocket-tts-coreml | `c2e998e269e4d4b51f74061faddb8450cd8f9a9a` | `v4/english_2m_2026-10/constants_bin/vera.safetensors` | 559,496 | 302 to 200 | false |
| FluidInference/pocket-tts-coreml | `c2e998e269e4d4b51f74061faddb8450cd8f9a9a` | `v4/english_2m_2026-10/flow_decoder_fused.mlmodelc/analytics/coremldata.bin` | 243 | 302 to 200 | false |
| FluidInference/pocket-tts-coreml | `c2e998e269e4d4b51f74061faddb8450cd8f9a9a` | `v4/english_2m_2026-10/flow_decoder_fused.mlmodelc/coremldata.bin` | 412 | 302 to 200 | false |
| FluidInference/pocket-tts-coreml | `c2e998e269e4d4b51f74061faddb8450cd8f9a9a` | `v4/english_2m_2026-10/flow_decoder_fused.mlmodelc/model.mil` | 30,053 | 307 to 200 | false |
| FluidInference/pocket-tts-coreml | `c2e998e269e4d4b51f74061faddb8450cd8f9a9a` | `v4/english_2m_2026-10/flow_decoder_fused.mlmodelc/weights/weight.bin` | 404,160 | 302 to 200 | false |
| FluidInference/pocket-tts-coreml | `c2e998e269e4d4b51f74061faddb8450cd8f9a9a` | `v4/english_2m_2026-10/flowlm_step.mlmodelc/analytics/coremldata.bin` | 243 | 302 to 200 | false |
| FluidInference/pocket-tts-coreml | `c2e998e269e4d4b51f74061faddb8450cd8f9a9a` | `v4/english_2m_2026-10/flowlm_step.mlmodelc/coremldata.bin` | 1,750 | 302 to 200 | false |
| FluidInference/pocket-tts-coreml | `c2e998e269e4d4b51f74061faddb8450cd8f9a9a` | `v4/english_2m_2026-10/flowlm_step.mlmodelc/model.mil` | 173,207 | 307 to 200 | false |
| FluidInference/pocket-tts-coreml | `c2e998e269e4d4b51f74061faddb8450cd8f9a9a` | `v4/english_2m_2026-10/flowlm_step.mlmodelc/weights/weight.bin` | 2,728,256 | 302 to 200 | false |
| FluidInference/pocket-tts-coreml | `c2e998e269e4d4b51f74061faddb8450cd8f9a9a` | `v4/english_2m_2026-10/manifest.json` | 7,015 | 307 to 200 | false |
| FluidInference/pocket-tts-coreml | `c2e998e269e4d4b51f74061faddb8450cd8f9a9a` | `v4/english_2m_2026-10/mimi_decoder.mlmodelc/analytics/coremldata.bin` | 243 | 302 to 200 | false |
| FluidInference/pocket-tts-coreml | `c2e998e269e4d4b51f74061faddb8450cd8f9a9a` | `v4/english_2m_2026-10/mimi_decoder.mlmodelc/coremldata.bin` | 2,460 | 302 to 200 | false |
| FluidInference/pocket-tts-coreml | `c2e998e269e4d4b51f74061faddb8450cd8f9a9a` | `v4/english_2m_2026-10/mimi_decoder.mlmodelc/model.mil` | 136,426 | 307 to 200 | false |
| FluidInference/pocket-tts-coreml | `c2e998e269e4d4b51f74061faddb8450cd8f9a9a` | `v4/english_2m_2026-10/mimi_decoder.mlmodelc/weights/weight.bin` | 4,393,504 | 302 to 200 | false |
| FluidInference/pocket-tts-coreml | `c2e998e269e4d4b51f74061faddb8450cd8f9a9a` | `v4/english_2m_2026-10/pocket_state.json` | 3,588 | 307 to 200 | false |
| FluidInference/pocket-tts-coreml | `c2e998e269e4d4b51f74061faddb8450cd8f9a9a` | `v4/english_2m_2026-10/pocket_state.mlmodelc/analytics/coremldata.bin` | 243 | 302 to 200 | false |
| FluidInference/pocket-tts-coreml | `c2e998e269e4d4b51f74061faddb8450cd8f9a9a` | `v4/english_2m_2026-10/pocket_state.mlmodelc/coremldata.bin` | 3,492 | 302 to 200 | false |
| FluidInference/pocket-tts-coreml | `c2e998e269e4d4b51f74061faddb8450cd8f9a9a` | `v4/english_2m_2026-10/pocket_state.mlmodelc/model.mil` | 744,220 | 307 to 200 | false |
| FluidInference/pocket-tts-coreml | `c2e998e269e4d4b51f74061faddb8450cd8f9a9a` | `v4/english_2m_2026-10/pocket_state.mlmodelc/weights/weight.bin` | 3,769,744 | 302 to 200 | false |
| FluidInference/pocket-tts-coreml | `c2e998e269e4d4b51f74061faddb8450cd8f9a9a` | `v4/english_7m_2026-10/arch.json` | 640 | 307 to 200 | false |
| FluidInference/pocket-tts-coreml | `c2e998e269e4d4b51f74061faddb8450cd8f9a9a` | `v4/english_7m_2026-10/cond_prefill.mlmodelc/analytics/coremldata.bin` | 243 | 302 to 200 | false |
| FluidInference/pocket-tts-coreml | `c2e998e269e4d4b51f74061faddb8450cd8f9a9a` | `v4/english_7m_2026-10/cond_prefill.mlmodelc/coremldata.bin` | 1,725 | 302 to 200 | false |
| FluidInference/pocket-tts-coreml | `c2e998e269e4d4b51f74061faddb8450cd8f9a9a` | `v4/english_7m_2026-10/cond_prefill.mlmodelc/model.mil` | 152,358 | 307 to 200 | false |
| FluidInference/pocket-tts-coreml | `c2e998e269e4d4b51f74061faddb8450cd8f9a9a` | `v4/english_7m_2026-10/cond_prefill.mlmodelc/weights/weight.bin` | 11,696,832 | 302 to 200 | false |
| FluidInference/pocket-tts-coreml | `c2e998e269e4d4b51f74061faddb8450cd8f9a9a` | `v4/english_7m_2026-10/constants/arch.json` | 640 | 307 to 200 | false |
| FluidInference/pocket-tts-coreml | `c2e998e269e4d4b51f74061faddb8450cd8f9a9a` | `v4/english_7m_2026-10/constants/bos_before_voice.npy` | 1,024 | 302 to 200 | false |
| FluidInference/pocket-tts-coreml | `c2e998e269e4d4b51f74061faddb8450cd8f9a9a` | `v4/english_7m_2026-10/constants/bos_emb.npy` | 256 | 302 to 200 | false |
| FluidInference/pocket-tts-coreml | `c2e998e269e4d4b51f74061faddb8450cd8f9a9a` | `v4/english_7m_2026-10/constants/emb_mean.npy` | 256 | 302 to 200 | false |
| FluidInference/pocket-tts-coreml | `c2e998e269e4d4b51f74061faddb8450cd8f9a9a` | `v4/english_7m_2026-10/constants/emb_std.npy` | 256 | 302 to 200 | false |
| FluidInference/pocket-tts-coreml | `c2e998e269e4d4b51f74061faddb8450cd8f9a9a` | `v4/english_7m_2026-10/constants/mimi_state_spec.json` | 6,763 | 307 to 200 | false |
| FluidInference/pocket-tts-coreml | `c2e998e269e4d4b51f74061faddb8450cd8f9a9a` | `v4/english_7m_2026-10/constants/speaker_proj_weight.npy` | 28,800 | 302 to 200 | false |
| FluidInference/pocket-tts-coreml | `c2e998e269e4d4b51f74061faddb8450cd8f9a9a` | `v4/english_7m_2026-10/constants/text_embed_table.npy` | 3,585,024 | 302 to 200 | false |
| FluidInference/pocket-tts-coreml | `c2e998e269e4d4b51f74061faddb8450cd8f9a9a` | `v4/english_7m_2026-10/constants_bin/alba.safetensors` | 1,042,832 | 302 to 200 | false |
| FluidInference/pocket-tts-coreml | `c2e998e269e4d4b51f74061faddb8450cd8f9a9a` | `v4/english_7m_2026-10/constants_bin/anna.safetensors` | 1,272,208 | 302 to 200 | false |
| FluidInference/pocket-tts-coreml | `c2e998e269e4d4b51f74061faddb8450cd8f9a9a` | `v4/english_7m_2026-10/constants_bin/azelma.safetensors` | 1,198,480 | 302 to 200 | false |
| FluidInference/pocket-tts-coreml | `c2e998e269e4d4b51f74061faddb8450cd8f9a9a` | `v4/english_7m_2026-10/constants_bin/bill_boerst.safetensors` | 1,091,984 | 302 to 200 | false |
| FluidInference/pocket-tts-coreml | `c2e998e269e4d4b51f74061faddb8450cd8f9a9a` | `v4/english_7m_2026-10/constants_bin/bos_before_voice.bin` | 896 | 302 to 200 | false |
| FluidInference/pocket-tts-coreml | `c2e998e269e4d4b51f74061faddb8450cd8f9a9a` | `v4/english_7m_2026-10/constants_bin/bos_emb.bin` | 128 | 302 to 200 | false |
| FluidInference/pocket-tts-coreml | `c2e998e269e4d4b51f74061faddb8450cd8f9a9a` | `v4/english_7m_2026-10/constants_bin/caro_davy.safetensors` | 846,224 | 302 to 200 | false |
| FluidInference/pocket-tts-coreml | `c2e998e269e4d4b51f74061faddb8450cd8f9a9a` | `v4/english_7m_2026-10/constants_bin/charles.safetensors` | 1,034,640 | 302 to 200 | false |
| FluidInference/pocket-tts-coreml | `c2e998e269e4d4b51f74061faddb8450cd8f9a9a` | `v4/english_7m_2026-10/constants_bin/cosette.safetensors` | 1,042,832 | 302 to 200 | false |
| FluidInference/pocket-tts-coreml | `c2e998e269e4d4b51f74061faddb8450cd8f9a9a` | `v4/english_7m_2026-10/constants_bin/daan.safetensors` | 649,600 | 302 to 200 | false |
| FluidInference/pocket-tts-coreml | `c2e998e269e4d4b51f74061faddb8450cd8f9a9a` | `v4/english_7m_2026-10/constants_bin/eponine.safetensors` | 1,157,520 | 302 to 200 | false |
| FluidInference/pocket-tts-coreml | `c2e998e269e4d4b51f74061faddb8450cd8f9a9a` | `v4/english_7m_2026-10/constants_bin/estelle.safetensors` | 1,264,016 | 302 to 200 | false |
| FluidInference/pocket-tts-coreml | `c2e998e269e4d4b51f74061faddb8450cd8f9a9a` | `v4/english_7m_2026-10/constants_bin/eve.safetensors` | 1,026,448 | 302 to 200 | false |
| FluidInference/pocket-tts-coreml | `c2e998e269e4d4b51f74061faddb8450cd8f9a9a` | `v4/english_7m_2026-10/constants_bin/fantine.safetensors` | 1,075,600 | 302 to 200 | false |
| FluidInference/pocket-tts-coreml | `c2e998e269e4d4b51f74061faddb8450cd8f9a9a` | `v4/english_7m_2026-10/constants_bin/george.safetensors` | 985,488 | 302 to 200 | false |
| FluidInference/pocket-tts-coreml | `c2e998e269e4d4b51f74061faddb8450cd8f9a9a` | `v4/english_7m_2026-10/constants_bin/giovanni.safetensors` | 690,560 | 302 to 200 | false |
| FluidInference/pocket-tts-coreml | `c2e998e269e4d4b51f74061faddb8450cd8f9a9a` | `v4/english_7m_2026-10/constants_bin/jane.safetensors` | 1,223,056 | 302 to 200 | false |
| FluidInference/pocket-tts-coreml | `c2e998e269e4d4b51f74061faddb8450cd8f9a9a` | `v4/english_7m_2026-10/constants_bin/javert.safetensors` | 1,026,448 | 302 to 200 | false |
| FluidInference/pocket-tts-coreml | `c2e998e269e4d4b51f74061faddb8450cd8f9a9a` | `v4/english_7m_2026-10/constants_bin/jean.safetensors` | 1,018,256 | 302 to 200 | false |
| FluidInference/pocket-tts-coreml | `c2e998e269e4d4b51f74061faddb8450cd8f9a9a` | `v4/english_7m_2026-10/constants_bin/juergen.safetensors` | 1,018,256 | 302 to 200 | false |
| FluidInference/pocket-tts-coreml | `c2e998e269e4d4b51f74061faddb8450cd8f9a9a` | `v4/english_7m_2026-10/constants_bin/lola.safetensors` | 911,760 | 302 to 200 | false |
| FluidInference/pocket-tts-coreml | `c2e998e269e4d4b51f74061faddb8450cd8f9a9a` | `v4/english_7m_2026-10/constants_bin/marius.safetensors` | 1,042,832 | 302 to 200 | false |
| FluidInference/pocket-tts-coreml | `c2e998e269e4d4b51f74061faddb8450cd8f9a9a` | `v4/english_7m_2026-10/constants_bin/mary.safetensors` | 993,680 | 302 to 200 | false |
| FluidInference/pocket-tts-coreml | `c2e998e269e4d4b51f74061faddb8450cd8f9a9a` | `v4/english_7m_2026-10/constants_bin/michael.safetensors` | 1,190,288 | 302 to 200 | false |
| FluidInference/pocket-tts-coreml | `c2e998e269e4d4b51f74061faddb8450cd8f9a9a` | `v4/english_7m_2026-10/constants_bin/paul.safetensors` | 1,141,136 | 302 to 200 | false |
| FluidInference/pocket-tts-coreml | `c2e998e269e4d4b51f74061faddb8450cd8f9a9a` | `v4/english_7m_2026-10/constants_bin/peter_yearsley.safetensors` | 608,640 | 302 to 200 | false |
| FluidInference/pocket-tts-coreml | `c2e998e269e4d4b51f74061faddb8450cd8f9a9a` | `v4/english_7m_2026-10/constants_bin/rafael.safetensors` | 1,042,832 | 302 to 200 | false |
| FluidInference/pocket-tts-coreml | `c2e998e269e4d4b51f74061faddb8450cd8f9a9a` | `v4/english_7m_2026-10/constants_bin/speaker_proj_weight.bin` | 28,672 | 302 to 200 | false |
| FluidInference/pocket-tts-coreml | `c2e998e269e4d4b51f74061faddb8450cd8f9a9a` | `v4/english_7m_2026-10/constants_bin/stuart_bell.safetensors` | 846,224 | 302 to 200 | false |
| FluidInference/pocket-tts-coreml | `c2e998e269e4d4b51f74061faddb8450cd8f9a9a` | `v4/english_7m_2026-10/constants_bin/text_embed_table.bin` | 3,584,896 | 302 to 200 | false |
| FluidInference/pocket-tts-coreml | `c2e998e269e4d4b51f74061faddb8450cd8f9a9a` | `v4/english_7m_2026-10/constants_bin/tokenizer.model` | 59,339 | 302 to 200 | false |
| FluidInference/pocket-tts-coreml | `c2e998e269e4d4b51f74061faddb8450cd8f9a9a` | `v4/english_7m_2026-10/constants_bin/vera.safetensors` | 1,116,560 | 302 to 200 | false |
| FluidInference/pocket-tts-coreml | `c2e998e269e4d4b51f74061faddb8450cd8f9a9a` | `v4/english_7m_2026-10/flow_decoder_fused.mlmodelc/analytics/coremldata.bin` | 243 | 302 to 200 | false |
| FluidInference/pocket-tts-coreml | `c2e998e269e4d4b51f74061faddb8450cd8f9a9a` | `v4/english_7m_2026-10/flow_decoder_fused.mlmodelc/coremldata.bin` | 412 | 302 to 200 | false |
| FluidInference/pocket-tts-coreml | `c2e998e269e4d4b51f74061faddb8450cd8f9a9a` | `v4/english_7m_2026-10/flow_decoder_fused.mlmodelc/model.mil` | 30,152 | 307 to 200 | false |
| FluidInference/pocket-tts-coreml | `c2e998e269e4d4b51f74061faddb8450cd8f9a9a` | `v4/english_7m_2026-10/flow_decoder_fused.mlmodelc/weights/weight.bin` | 3,369,664 | 302 to 200 | false |
| FluidInference/pocket-tts-coreml | `c2e998e269e4d4b51f74061faddb8450cd8f9a9a` | `v4/english_7m_2026-10/flowlm_step.mlmodelc/analytics/coremldata.bin` | 243 | 302 to 200 | false |
| FluidInference/pocket-tts-coreml | `c2e998e269e4d4b51f74061faddb8450cd8f9a9a` | `v4/english_7m_2026-10/flowlm_step.mlmodelc/coremldata.bin` | 1,750 | 302 to 200 | false |
| FluidInference/pocket-tts-coreml | `c2e998e269e4d4b51f74061faddb8450cd8f9a9a` | `v4/english_7m_2026-10/flowlm_step.mlmodelc/model.mil` | 171,429 | 307 to 200 | false |
| FluidInference/pocket-tts-coreml | `c2e998e269e4d4b51f74061faddb8450cd8f9a9a` | `v4/english_7m_2026-10/flowlm_step.mlmodelc/weights/weight.bin` | 12,648,320 | 302 to 200 | false |
| FluidInference/pocket-tts-coreml | `c2e998e269e4d4b51f74061faddb8450cd8f9a9a` | `v4/english_7m_2026-10/manifest.json` | 9,092 | 307 to 200 | false |
| FluidInference/pocket-tts-coreml | `c2e998e269e4d4b51f74061faddb8450cd8f9a9a` | `v4/english_7m_2026-10/mimi_decoder.mlmodelc/analytics/coremldata.bin` | 243 | 302 to 200 | false |
| FluidInference/pocket-tts-coreml | `c2e998e269e4d4b51f74061faddb8450cd8f9a9a` | `v4/english_7m_2026-10/mimi_decoder.mlmodelc/coremldata.bin` | 4,968 | 302 to 200 | false |
| FluidInference/pocket-tts-coreml | `c2e998e269e4d4b51f74061faddb8450cd8f9a9a` | `v4/english_7m_2026-10/mimi_decoder.mlmodelc/model.mil` | 306,570 | 307 to 200 | false |
| FluidInference/pocket-tts-coreml | `c2e998e269e4d4b51f74061faddb8450cd8f9a9a` | `v4/english_7m_2026-10/mimi_decoder.mlmodelc/weights/weight.bin` | 10,701,824 | 302 to 200 | false |
| FluidInference/pocket-tts-coreml | `c2e998e269e4d4b51f74061faddb8450cd8f9a9a` | `v4/english_7m_2026-10/pocket_state.json` | 5,467 | 307 to 200 | false |
| FluidInference/pocket-tts-coreml | `c2e998e269e4d4b51f74061faddb8450cd8f9a9a` | `v4/english_7m_2026-10/pocket_state.mlmodelc/analytics/coremldata.bin` | 243 | 302 to 200 | false |
| FluidInference/pocket-tts-coreml | `c2e998e269e4d4b51f74061faddb8450cd8f9a9a` | `v4/english_7m_2026-10/pocket_state.mlmodelc/coremldata.bin` | 8,562 | 302 to 200 | false |
| FluidInference/pocket-tts-coreml | `c2e998e269e4d4b51f74061faddb8450cd8f9a9a` | `v4/english_7m_2026-10/pocket_state.mlmodelc/model.mil` | 1,359,587 | 307 to 200 | false |
| FluidInference/pocket-tts-coreml | `c2e998e269e4d4b51f74061faddb8450cd8f9a9a` | `v4/english_7m_2026-10/pocket_state.mlmodelc/weights/weight.bin` | 13,369,536 | 302 to 200 | false |
| Kentucky-Open-Science/Rumble-2-Flash-Astral | `573fa2b486f6e5f134f35350b3256fe8977ee826` | `astral-manifest.json` | 61,243 | 307 to 200 | false |
| Kentucky-Open-Science/Rumble-2-Flash-Astral | `573fa2b486f6e5f134f35350b3256fe8977ee826` | `chat-1.2b/LFM2.5-1.2B-Instruct-Q4_K_M.gguf` | 730,895,168 | 302 to 200 | false |
| Kentucky-Open-Science/Rumble-2-Flash-Astral | `573fa2b486f6e5f134f35350b3256fe8977ee826` | `download_models.py` | 4,629 | 307 to 200 | false |
| Kentucky-Open-Science/Rumble-2-Flash-Astral | `573fa2b486f6e5f134f35350b3256fe8977ee826` | `licenses/Liquid-LFM-Open-License.txt` | 10,596 | 307 to 200 | false |
| Kentucky-Open-Science/Rumble-2-Flash-Astral | `573fa2b486f6e5f134f35350b3256fe8977ee826` | `licenses/Rumble-Apache-2.0.txt` | 11,358 | 307 to 200 | false |
| Kentucky-Open-Science/Rumble-2-Flash-Astral | `573fa2b486f6e5f134f35350b3256fe8977ee826` | `model-hashes.json` | 37,842 | 307 to 200 | false |
| Kentucky-Open-Science/Rumble-2-Flash-Astral | `573fa2b486f6e5f134f35350b3256fe8977ee826` | `rumble/Rumble-2-Flash-AD-Q5_K_M.gguf` | 283,424,640 | 302 to 200 | false |
| Kentucky-Open-Science/Rumble-2-Flash-Astral | `573fa2b486f6e5f134f35350b3256fe8977ee826` | `rumble/mmproj-Rumble-2-Flash-Q8_0.gguf` | 214,393,024 | 302 to 200 | false |
| AtomicChat/Rumble-2-Flash-GGUF | `d036f2af3cd5229375714d7f1749792cce3c6a88` | `Rumble-2-Flash-AD-Q5_K_M.gguf` | 283,424,640 | 302 to 200 | false |
| AtomicChat/Rumble-2-Flash-GGUF | `d036f2af3cd5229375714d7f1749792cce3c6a88` | `mmproj-Rumble-2-Flash-Q8_0.gguf` | 214,393,024 | 302 to 200 | false |
| LiquidAI/LFM2.5-1.2B-Instruct-GGUF | `8ed288026e23958ad9dfa92d53ed773a8eee7125` | `LFM2.5-1.2B-Instruct-Q4_K_M.gguf` | 730,895,168 | 302 to 200 | false |
| kyutai/pocket-tts | `db04f0e653473e5c7333c69686ac92b7460b0c1d` | `README.md` | 26,699 | 200 to 200 | auto |
| kyutai/pocket-tts | `db04f0e653473e5c7333c69686ac92b7460b0c1d` | `embeddings_v3/alba.safetensors` | 6,145,272 | 401 to 401 | auto |
| kyutai/pocket-tts | `db04f0e653473e5c7333c69686ac92b7460b0c1d` | `languages/english_18m_2026-10/embeddings/alba.safetensors` | 4,163,992 | 401 to 401 | auto |
| kyutai/pocket-tts | `db04f0e653473e5c7333c69686ac92b7460b0c1d` | `languages/english_18m_2026-10/embeddings/anna.safetensors` | 5,081,496 | 401 to 401 | auto |
| kyutai/pocket-tts | `db04f0e653473e5c7333c69686ac92b7460b0c1d` | `languages/english_18m_2026-10/embeddings/azelma.safetensors` | 4,786,584 | 401 to 401 | auto |
| kyutai/pocket-tts | `db04f0e653473e5c7333c69686ac92b7460b0c1d` | `languages/english_18m_2026-10/embeddings/bill_boerst.safetensors` | 4,360,600 | 401 to 401 | auto |
| kyutai/pocket-tts | `db04f0e653473e5c7333c69686ac92b7460b0c1d` | `languages/english_18m_2026-10/embeddings/caro_davy.safetensors` | 3,377,560 | 401 to 401 | auto |
| kyutai/pocket-tts | `db04f0e653473e5c7333c69686ac92b7460b0c1d` | `languages/english_18m_2026-10/embeddings/charles.safetensors` | 4,131,224 | 401 to 401 | auto |
| kyutai/pocket-tts | `db04f0e653473e5c7333c69686ac92b7460b0c1d` | `languages/english_18m_2026-10/embeddings/cosette.safetensors` | 4,163,992 | 401 to 401 | auto |
| kyutai/pocket-tts | `db04f0e653473e5c7333c69686ac92b7460b0c1d` | `languages/english_18m_2026-10/embeddings/daan.safetensors` | 2,591,120 | 401 to 401 | auto |
| kyutai/pocket-tts | `db04f0e653473e5c7333c69686ac92b7460b0c1d` | `languages/english_18m_2026-10/embeddings/eponine.safetensors` | 4,622,744 | 401 to 401 | auto |
| kyutai/pocket-tts | `db04f0e653473e5c7333c69686ac92b7460b0c1d` | `languages/english_18m_2026-10/embeddings/estelle.safetensors` | 5,048,728 | 401 to 401 | auto |
| kyutai/pocket-tts | `db04f0e653473e5c7333c69686ac92b7460b0c1d` | `languages/english_18m_2026-10/embeddings/eve.safetensors` | 4,098,456 | 401 to 401 | auto |
| kyutai/pocket-tts | `db04f0e653473e5c7333c69686ac92b7460b0c1d` | `languages/english_18m_2026-10/embeddings/fantine.safetensors` | 4,295,064 | 401 to 401 | auto |
| kyutai/pocket-tts | `db04f0e653473e5c7333c69686ac92b7460b0c1d` | `languages/english_18m_2026-10/embeddings/george.safetensors` | 3,934,616 | 401 to 401 | auto |
| kyutai/pocket-tts | `db04f0e653473e5c7333c69686ac92b7460b0c1d` | `languages/english_18m_2026-10/embeddings/giovanni.safetensors` | 2,754,960 | 401 to 401 | auto |
| kyutai/pocket-tts | `db04f0e653473e5c7333c69686ac92b7460b0c1d` | `languages/english_18m_2026-10/embeddings/jane.safetensors` | 4,884,888 | 401 to 401 | auto |
| kyutai/pocket-tts | `db04f0e653473e5c7333c69686ac92b7460b0c1d` | `languages/english_18m_2026-10/embeddings/javert.safetensors` | 4,098,456 | 401 to 401 | auto |
| kyutai/pocket-tts | `db04f0e653473e5c7333c69686ac92b7460b0c1d` | `languages/english_18m_2026-10/embeddings/jean.safetensors` | 4,065,688 | 401 to 401 | auto |
| kyutai/pocket-tts | `db04f0e653473e5c7333c69686ac92b7460b0c1d` | `languages/english_18m_2026-10/embeddings/juergen.safetensors` | 4,065,688 | 401 to 401 | auto |
| kyutai/pocket-tts | `db04f0e653473e5c7333c69686ac92b7460b0c1d` | `languages/english_18m_2026-10/embeddings/lola.safetensors` | 3,639,704 | 401 to 401 | auto |
| kyutai/pocket-tts | `db04f0e653473e5c7333c69686ac92b7460b0c1d` | `languages/english_18m_2026-10/embeddings/marius.safetensors` | 4,163,992 | 401 to 401 | auto |
| kyutai/pocket-tts | `db04f0e653473e5c7333c69686ac92b7460b0c1d` | `languages/english_18m_2026-10/embeddings/mary.safetensors` | 3,967,384 | 401 to 401 | auto |
| kyutai/pocket-tts | `db04f0e653473e5c7333c69686ac92b7460b0c1d` | `languages/english_18m_2026-10/embeddings/michael.safetensors` | 4,753,816 | 401 to 401 | auto |
| kyutai/pocket-tts | `db04f0e653473e5c7333c69686ac92b7460b0c1d` | `languages/english_18m_2026-10/embeddings/paul.safetensors` | 4,557,208 | 401 to 401 | auto |
| kyutai/pocket-tts | `db04f0e653473e5c7333c69686ac92b7460b0c1d` | `languages/english_18m_2026-10/embeddings/peter_yearsley.safetensors` | 2,427,280 | 401 to 401 | auto |
| kyutai/pocket-tts | `db04f0e653473e5c7333c69686ac92b7460b0c1d` | `languages/english_18m_2026-10/embeddings/rafael.safetensors` | 4,163,992 | 401 to 401 | auto |
| kyutai/pocket-tts | `db04f0e653473e5c7333c69686ac92b7460b0c1d` | `languages/english_18m_2026-10/embeddings/stuart_bell.safetensors` | 3,377,560 | 401 to 401 | auto |
| kyutai/pocket-tts | `db04f0e653473e5c7333c69686ac92b7460b0c1d` | `languages/english_18m_2026-10/embeddings/vera.safetensors` | 4,458,904 | 401 to 401 | auto |
| kyutai/pocket-tts | `db04f0e653473e5c7333c69686ac92b7460b0c1d` | `languages/english_18m_2026-10/model.safetensors` | 111,728,528 | 401 to 401 | auto |
| kyutai/pocket-tts | `db04f0e653473e5c7333c69686ac92b7460b0c1d` | `languages/english_18m_2026-10/tokenizer.json` | 245,020 | 401 to 401 | auto |
| kyutai/pocket-tts | `db04f0e653473e5c7333c69686ac92b7460b0c1d` | `languages/english_18m_2026-10/tokenizer.model` | 59,339 | 401 to 401 | auto |
| kyutai/pocket-tts | `db04f0e653473e5c7333c69686ac92b7460b0c1d` | `languages/english_18m_refl_2026-10/embeddings/alba.safetensors` | 4,163,992 | 401 to 401 | auto |
| kyutai/pocket-tts | `db04f0e653473e5c7333c69686ac92b7460b0c1d` | `languages/english_18m_refl_2026-10/embeddings/anna.safetensors` | 5,081,496 | 401 to 401 | auto |
| kyutai/pocket-tts | `db04f0e653473e5c7333c69686ac92b7460b0c1d` | `languages/english_18m_refl_2026-10/embeddings/azelma.safetensors` | 4,786,584 | 401 to 401 | auto |
| kyutai/pocket-tts | `db04f0e653473e5c7333c69686ac92b7460b0c1d` | `languages/english_18m_refl_2026-10/embeddings/bill_boerst.safetensors` | 4,360,600 | 401 to 401 | auto |
| kyutai/pocket-tts | `db04f0e653473e5c7333c69686ac92b7460b0c1d` | `languages/english_18m_refl_2026-10/embeddings/caro_davy.safetensors` | 3,377,560 | 401 to 401 | auto |
| kyutai/pocket-tts | `db04f0e653473e5c7333c69686ac92b7460b0c1d` | `languages/english_18m_refl_2026-10/embeddings/charles.safetensors` | 4,131,224 | 401 to 401 | auto |
| kyutai/pocket-tts | `db04f0e653473e5c7333c69686ac92b7460b0c1d` | `languages/english_18m_refl_2026-10/embeddings/cosette.safetensors` | 4,163,992 | 401 to 401 | auto |
| kyutai/pocket-tts | `db04f0e653473e5c7333c69686ac92b7460b0c1d` | `languages/english_18m_refl_2026-10/embeddings/daan.safetensors` | 2,591,120 | 401 to 401 | auto |
| kyutai/pocket-tts | `db04f0e653473e5c7333c69686ac92b7460b0c1d` | `languages/english_18m_refl_2026-10/embeddings/eponine.safetensors` | 4,622,744 | 401 to 401 | auto |
| kyutai/pocket-tts | `db04f0e653473e5c7333c69686ac92b7460b0c1d` | `languages/english_18m_refl_2026-10/embeddings/estelle.safetensors` | 5,048,728 | 401 to 401 | auto |
| kyutai/pocket-tts | `db04f0e653473e5c7333c69686ac92b7460b0c1d` | `languages/english_18m_refl_2026-10/embeddings/eve.safetensors` | 4,098,456 | 401 to 401 | auto |
| kyutai/pocket-tts | `db04f0e653473e5c7333c69686ac92b7460b0c1d` | `languages/english_18m_refl_2026-10/embeddings/fantine.safetensors` | 4,295,064 | 401 to 401 | auto |
| kyutai/pocket-tts | `db04f0e653473e5c7333c69686ac92b7460b0c1d` | `languages/english_18m_refl_2026-10/embeddings/george.safetensors` | 3,934,616 | 401 to 401 | auto |
| kyutai/pocket-tts | `db04f0e653473e5c7333c69686ac92b7460b0c1d` | `languages/english_18m_refl_2026-10/embeddings/giovanni.safetensors` | 2,754,960 | 401 to 401 | auto |
| kyutai/pocket-tts | `db04f0e653473e5c7333c69686ac92b7460b0c1d` | `languages/english_18m_refl_2026-10/embeddings/jane.safetensors` | 4,884,888 | 401 to 401 | auto |
| kyutai/pocket-tts | `db04f0e653473e5c7333c69686ac92b7460b0c1d` | `languages/english_18m_refl_2026-10/embeddings/javert.safetensors` | 4,098,456 | 401 to 401 | auto |
| kyutai/pocket-tts | `db04f0e653473e5c7333c69686ac92b7460b0c1d` | `languages/english_18m_refl_2026-10/embeddings/jean.safetensors` | 4,065,688 | 401 to 401 | auto |
| kyutai/pocket-tts | `db04f0e653473e5c7333c69686ac92b7460b0c1d` | `languages/english_18m_refl_2026-10/embeddings/juergen.safetensors` | 4,065,688 | 401 to 401 | auto |
| kyutai/pocket-tts | `db04f0e653473e5c7333c69686ac92b7460b0c1d` | `languages/english_18m_refl_2026-10/embeddings/lola.safetensors` | 3,639,704 | 401 to 401 | auto |
| kyutai/pocket-tts | `db04f0e653473e5c7333c69686ac92b7460b0c1d` | `languages/english_18m_refl_2026-10/embeddings/marius.safetensors` | 4,163,992 | 401 to 401 | auto |
| kyutai/pocket-tts | `db04f0e653473e5c7333c69686ac92b7460b0c1d` | `languages/english_18m_refl_2026-10/embeddings/mary.safetensors` | 3,967,384 | 401 to 401 | auto |
| kyutai/pocket-tts | `db04f0e653473e5c7333c69686ac92b7460b0c1d` | `languages/english_18m_refl_2026-10/embeddings/michael.safetensors` | 4,753,816 | 401 to 401 | auto |
| kyutai/pocket-tts | `db04f0e653473e5c7333c69686ac92b7460b0c1d` | `languages/english_18m_refl_2026-10/embeddings/paul.safetensors` | 4,557,208 | 401 to 401 | auto |
| kyutai/pocket-tts | `db04f0e653473e5c7333c69686ac92b7460b0c1d` | `languages/english_18m_refl_2026-10/embeddings/peter_yearsley.safetensors` | 2,427,280 | 401 to 401 | auto |
| kyutai/pocket-tts | `db04f0e653473e5c7333c69686ac92b7460b0c1d` | `languages/english_18m_refl_2026-10/embeddings/rafael.safetensors` | 4,163,992 | 401 to 401 | auto |
| kyutai/pocket-tts | `db04f0e653473e5c7333c69686ac92b7460b0c1d` | `languages/english_18m_refl_2026-10/embeddings/stuart_bell.safetensors` | 3,377,560 | 401 to 401 | auto |
| kyutai/pocket-tts | `db04f0e653473e5c7333c69686ac92b7460b0c1d` | `languages/english_18m_refl_2026-10/embeddings/vera.safetensors` | 4,458,904 | 401 to 401 | auto |
| kyutai/pocket-tts | `db04f0e653473e5c7333c69686ac92b7460b0c1d` | `languages/english_18m_refl_2026-10/model.safetensors` | 111,728,528 | 401 to 401 | auto |
| kyutai/pocket-tts | `db04f0e653473e5c7333c69686ac92b7460b0c1d` | `languages/english_18m_refl_2026-10/tokenizer.json` | 245,020 | 401 to 401 | auto |
| kyutai/pocket-tts | `db04f0e653473e5c7333c69686ac92b7460b0c1d` | `languages/english_18m_refl_2026-10/tokenizer.model` | 59,339 | 401 to 401 | auto |
| kyutai/pocket-tts-without-voice-cloning | `a7056f505a4a8ca72d8b5fb1440071d6f1a35739` | `README.md` | 26,699 | 307 to 200 | false |
| kyutai/pocket-tts-without-voice-cloning | `a7056f505a4a8ca72d8b5fb1440071d6f1a35739` | `embeddings_v3/alba.safetensors` | 6,145,272 | 302 to 200 | false |
| gradium/phonon-31m-en | `8f7a37c94a4c58b04c8287300c53f11d2e208eae` | `LICENSE` | 11,357 | 307 to 200 | false |
| gradium/phonon-31m-en | `8f7a37c94a4c58b04c8287300c53f11d2e208eae` | `default-voice.safetensors` | 256,136 | 302 to 200 | false |
| gradium/phonon-31m-en | `8f7a37c94a4c58b04c8287300c53f11d2e208eae` | `model.safetensors` | 102,823,920 | 302 to 200 | false |
