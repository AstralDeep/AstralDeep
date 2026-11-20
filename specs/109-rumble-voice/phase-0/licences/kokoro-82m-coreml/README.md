---
license: apache-2.0
language:
  - en
  - ja
  - zh
base_model:
- hexgrad/Kokoro-82M
- hexgrad/Kokoro-82M-v1.1-zh
pipeline_tag: text-to-speech
---

Core ML conversions of **[hexgrad/Kokoro-82M](https://huggingface.co/hexgrad/Kokoro-82M)** (English and Japanese) and **[hexgrad/Kokoro-82M-v1.1-zh](https://huggingface.co/hexgrad/Kokoro-82M-v1.1-zh)** (Mandarin). The original Kokoro models and pretrained weights are by [hexgrad](https://huggingface.co/hexgrad); this repository provides Apple Core ML conversions and runtime optimizations. See [FluidAudio](https://github.com/FluidInference/FluidAudio) for inference.

## ANE version 3 (opt-in)

Available for [English](ANE-v3/README.md), [Japanese](ANE-v3/ja/README.md)
and [Mandarin](ANE-v3/zh/README.md), with fixed Albert/decoder buckets,
native source DSP and a GPU generator adapted from Matt Mireles's conversion.
Existing model paths and defaults remain available.

## FluidAudio SDK v0.17.6

Use `KokoroAneManager(version: .v3)` for English,
`KokoroAneManager(variant: .japanese, version: .v3)` for Japanese, or
`KokoroAneManager(variant: .mandarin, version: .v3)` for Mandarin.
All three accept native text and variant voices. Existing callers retain
`.legacy`. See [SDK usage, voices and platform limits](https://github.com/FluidInference/FluidAudio/blob/d7931323eebea4e39874b0d2e75d1b0e90e13084/Documentation/TTS/KokoroAne.md).
Requires macOS 15 / iOS 18; physical iOS inference remains unvalidated.

The SDK pins model release `6c0750dd02ef9d981fb82f147d784a3f171755ee` and base
revision `006395f65025af251858b1ab0a7178a6a1e73f9f`, adopting fp32
`KokoroProsody_v2` and COLA-corrected `KokoroTail_v2`. The bundled standalone
conversion runtime and its historical measurements below are separate from this
SDK integration. This documentation update does not change model bytes or pins.

### SDK compute routing and selected measurements

Hybrid is **not `.all` or ANE-only**. Albert, PostAlbert, Alignment, Prosody and
masked decoder request `.cpuAndNeuralEngine`; the fast generator requests
`.cpuAndGPU`; native source/STFT runs on CPU/Accelerate. Fallback Vocoder requests
CPU+ANE, Noise/Tail CPU+GPU. Core ML chooses actual placement within each policy.

| Policy | English | Japanese | Mandarin |
| --- | ---: | ---: | ---: |
| Current hybrid | **49.45 ms** | **35.85 ms** | **53.97 ms** |
| CPU+ANE throughout | 511.40 ms | 474.45 ms | 584.19 ms |
| `.all` throughout | SIGABRT | SIGABRT | SIGABRT |
| CPU+GPU throughout | SIGABRT | SIGABRT | SIGABRT |

Apple M5 Pro, 24 GiB, macOS 27.0 (26A428); October 7, 2026 (Toronto).
Median of five calls after two warmups on one selected demo input per language;
3.350 / 3.050 / 3.675 seconds of output. Timer covers prepared tokens/style to
complete PCM, excluding text processing, loading and WAV IO. Identical inputs
per policy, serial isolated processes, native DSP unchanged. These are local
exploratory checks, not corpus or cross-device results. Global overrides were
applied in a local harness; the public v3 API retains its fixed hybrid routing.
Both `.all` and CPU+GPU aborted during Prosody on the second warmup with Apple's
`GPURNNOps: JIT not supported`. CPU+ANE permits CPU fallback; it is not ANE-only.

Live-demo screenshots instead time raw text through complete audio and showed
57.8–79.8 ms in selected serial runs. Their 1.88× / 5.29× / 2.25× speedups versus
legacy are individual observations, not a benchmark average. Concurrent demo
mode also includes shared-resource contention.
See [inputs, ranges, protocol and raw records](https://github.com/FluidInference/FluidAudio/blob/d7931323eebea4e39874b0d2e75d1b0e90e13084/Documentation/TTS/Benchmarks.md#kokoro-ane-v3-selected-m5-pro-measurements).

Compute plans prefer ANE for static Albert/decoder and GPU for the generator,
but plan estimates and system-wide asitop power samples do not establish actual
per-process execution. See [ANE profiling and telemetry limits](https://github.com/FluidInference/FluidAudio/blob/d7931323eebea4e39874b0d2e75d1b0e90e13084/Documentation/ANE_Profiler.md#kokoro-ane-v3).

## Changelog

Fixed models are uploaded under a new name (`_v2`, …) next to the originals so older FluidAudio releases keep working and cached clients re-download. Each row links the issue, the conversion change (mobius), the Swift change (FluidAudio) and the HF commit.

| Date | Files | Change | Issue | mobius | FluidAudio | HF commit |
|---|---|---|---|---|---|---|
| 2026-11-08 | `ANE*/KokoroVocoder_v2.{mlmodelc,mlpackage}` | fp32 compute: the fp16 Core ML CPU path on SME chips (A19 Pro, A20, M5) corrupted the first ~1 s of long utterances (robotic onset) | [#1135](https://github.com/FluidInference/FluidAudio/issues/1135) | – (model-lab `kokoro-vocoder-v2-coreml`) | [#1137](https://github.com/FluidInference/FluidAudio/pull/1137) | [89102029](https://huggingface.co/FluidInference/kokoro-82m-coreml/commit/89102029b6d998c48a76a613805905a9704f1055) |
| 2026-10-28 | `gb_lexicon_cache.json` | British lexicon cache (misaki `gb_gold` + `gb_silver`, `us_lexicon_cache.json` format) for Paradee's British voices | [#1072](https://github.com/FluidInference/FluidAudio/issues/1072) | [#114](https://github.com/FluidInference/mobius/pull/114) | [#1073](https://github.com/FluidInference/FluidAudio/pull/1073) | [5c321779](https://huggingface.co/FluidInference/kokoro-82m-coreml/commit/5c321779d6aa39d09e2b0e90d369f8071b5ce31c) |
| 2026-09-25 | `ANE*/KokoroProsody_v2.mlmodelc` | fp32 compute: fp16 CPU/ANE corrupted F0/N at the utterance onset for T_a ≥ ~400 (quiet first words) | [#947](https://github.com/FluidInference/FluidAudio/issues/947) | [#108](https://github.com/FluidInference/mobius/pull/108) | [#963](https://github.com/FluidInference/FluidAudio/pull/963) | [9db203c5](https://huggingface.co/FluidInference/kokoro-82m-coreml/commit/9db203c5bd9f40052ae06e9a64af6ee9b07a72ab) |
| 2026-09-23 | `ANE/{es,fr}_lexicon_cache.json` | Spanish / French lexicon caches | [#926](https://github.com/FluidInference/FluidAudio/issues/926) | – | [#950](https://github.com/FluidInference/FluidAudio/pull/950) | [c9223c6c](https://huggingface.co/FluidInference/kokoro-82m-coreml/commit/c9223c6cd5df931d97ce1a0dd2e20b36aabb2946) |
| 2026-09-13 | `ANE-ja/g2p/*` | Japanese G2P assets (trimmed unidic-lite + Cutlet) | [#914](https://github.com/FluidInference/FluidAudio/issues/914) | [#90](https://github.com/FluidInference/mobius/pull/90) | [#915](https://github.com/FluidInference/FluidAudio/pull/915) | [c9fdfdac](https://huggingface.co/FluidInference/kokoro-82m-coreml/commit/c9fdfdac6ffee291414b0c163ac61aaa57d66f68) |
| 2026-08-19 | `ANE*/KokoroTail_v2.mlmodelc` | COLA-normalized iSTFT (output was 1.5× reference) | [#852](https://github.com/FluidInference/FluidAudio/issues/852) | [#83](https://github.com/FluidInference/mobius/pull/83) | [#868](https://github.com/FluidInference/FluidAudio/pull/868) | [acac8811](https://huggingface.co/FluidInference/kokoro-82m-coreml/commit/acac8811a9acefe8bf7a5e3fcba99bd8fc50dcd6) |
| 2026-08-19 | `ANE*/KokoroVocoder.mlmodelc` | rank-stable anchor for macOS 14 Espresso | [#836](https://github.com/FluidInference/FluidAudio/issues/836) | [#82](https://github.com/FluidInference/mobius/pull/82) | – | [cdaea5e2](https://huggingface.co/FluidInference/kokoro-82m-coreml/commit/cdaea5e293bf8aba9c60b6891088e4efc20b8fc3) |

## Historical M4 Pro benchmark

We generated the same strings with to gerneate audio between 1s to ~300s in order to test the speed across a range of varying inputs on Pytorch CPU, MPS, and MLX pipeline, and compared it against the native Swift version with Core ML models.

Each pipeline warmed up the models by running through it once with pesudo inputs, and then comparing the raw inference time with the model already loaded. You can see that for the Core ML model, we traded lower memory and very slightly faster inference for longer initial warm-up.

Note that the Pytorch kokoro model in Pytorch has a memory leak issue: https://github.com/hexgrad/kokoro/issues/152

The following tests were ran on M4 Pro, 48GB RAM, Macbook Pro. If you have another device, please do try replicating it as well!

### Kokoro-82M PyTorch (CPU)

```bash
KPipeline benchmark for voice af_heart (warm-up took 0.175s) using hexgrad/kokoro
Test   Chars    Output (s)   Inf(s)       RTFx       Peak GB
1      42       2.750        0.187        14.737x    1.44
2      129      8.625        0.530        16.264x    1.85
3      254      15.525       0.923        16.814x    2.65
4      93       6.125        0.349        17.566x    2.66
5      104      7.200        0.410        17.567x    2.70
6      130      9.300        0.504        18.443x    2.72
7      197      12.850       0.726        17.711x    2.83
8      6        1.350        0.098        13.823x    2.83
9      1228     76.200       4.342        17.551x    3.19
10     567      35.200       2.069        17.014x    4.85
11     4615     286.525      17.041       16.814x    4.78
Total  -        461.650      27.177       16.987x    4.85    
```

### Kokoro-82M PyTorch (MPS)

I wasn't able to run the MPS model for longer durations, even with `PYTORCH_ENABLE_MPS_FALLBACK=1` enabled, it kept crashing for the longer strings.

```bash
KPipeline benchmark for voice af_heart (warm-up took 0.568s) using pip package
Test   Chars    Output (s)   Inf(s)       RTFx       Peak GB
1      42       2.750        0.414        6.649x     1.41
2      129      8.625        0.729        11.839x    1.54
Total  -        11.375       1.142        9.960x     1.54    
```

### Kokoro-82M MLX Pipeline

```bash
TTS benchmark for voice af_heart (warm-up took an extra 2.155s) using model prince-canuma/Kokoro-82M
Test   Chars    Output (s)   Inf(s)       RTFx       Peak GB
1      42       2.750        0.347        7.932x     1.12
2      129      8.650        0.597        14.497x    2.47
3      254      15.525       0.825        18.829x    2.65
4      93       6.125        0.306        20.039x    2.65
5      104      7.200        0.343        21.001x    2.65
6      130      9.300        0.560        16.611x    2.65
7      197      12.850       0.596        21.573x    2.65
8      6        1.350        0.364        3.706x     2.65
9      1228     76.200       2.979        25.583x    3.29
10     567      35.200       1.374        25.615x    3.37
11     4615     286.500      11.112       25.783x    3.37
Total  -        461.650      19.401       23.796x    3.37
```

#### Swift + Fluid Audio Core ML models

Note that it does take `~15s` to compile the model on the first run, subsequent runs are shorter, we expect ~2s to load. 

```bash
> swift run fluidaudio tts --benchmark
...
FluidAudio TTS benchmark for voice af_heart (warm-up took an extra 2.348s)
Test   Chars    Ouput (s)    Inf(s)       RTFx
1      42       2.825        0.440        6.424x
2      129      7.725        0.594        13.014x
3      254      13.400       0.776        17.278x
4      93       5.875        0.587        10.005x
5      104      6.675        0.613        10.889x
6      130      8.075        0.621        13.008x
7      197      10.650       0.627        16.983x
8      6        0.825        0.360        2.290x
9      1228     67.625       2.362        28.625x
10     567      33.025       1.341        24.619x
11     4269     247.600      9.087        27.248x
Total  -        404.300      17.408       23.225

Peak memory usage (process-wide): 1.503 GB
```

#### Credtis
special thanks to @laishere for making kokoro ANE possible his source: https://github.com/laishere/kokoro-coreml 
