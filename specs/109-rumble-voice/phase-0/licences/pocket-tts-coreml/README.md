---
license: cc-by-4.0
library_name: coreml
tags:
- tts
- text-to-speech
- coreml
- apple
- on-device
- phonon
language:
- en
pipeline_tag: text-to-speech
base_model:
- kyutai/pocket-tts
base_model_relation: finetune
---

# PocketTTS CoreML

CoreML conversion of [kyutai/pocket-tts](https://huggingface.co/kyutai/pocket-tts) for on-device
inference on Apple platforms.

## Models

| Model | Description | Size |
|-------|-------------|------|
| cond_step | KV cache prefill (voice + text conditioning) | ~200MB |
| flowlm_step | Autoregressive generation (transformer_out + EOS) | ~200MB |
| flow_decoder | Flow matching denoiser (8 Euler steps per frame) | ~190MB |
| mimi_decoder | Streaming audio codec (1920 samples per frame) | ~11MB |

## Voices

4 pre-encoded voices in `constants_bin/`:
- `alba` (default), `azelma`, `cosette`, `javert`

Voice cloning is supported: the Mimi encoders are included (`mimi_encoder*.mlmodelc` at the root for English, `v2.1/<lang>/mimi_encoderv3.mlmodelc` per language pack). In FluidAudio, use `PocketTtsManager.cloneVoice()` or `fluidaudiocli tts "…" --backend pocket --clone-voice speaker.wav`.

## English 100M: `v2.1/english_2026-10/`

Kyutai's `english_2026-10`, upstream's current default English model (its `english` id
serves the same weights), in the v2.1 layout: fused 8-step flow decoder on the Neural
Engine, one-shot `cond_prefill`, fp16 / int8 / ANE FlowLM steps, the MLState
`pocket_state` package, and 27 voices. It reads sentence-final numbers and years; Kyutai's
config sets its default temperature to 0.3. Same architecture, tokenizer and Mimi codec
as `v2.1/english/` (Kyutai `english_2026-04`), whose `mimi_decoder` it reuses. FluidAudio's
`.english` loads this pack; the April pack stays in `v2.1/english/` as `.english2026Apr`.

M5 Pro, FluidAudio, voice alba, Parakeet WER on 100 MiniMax English phrases: 0.75 %
(PyTorch reference 0.86 %, April pack 0.80 %).

## Small models (pocket-tts 4.0.0): `v4/<model>/`

Core ML packs for Kyutai's six small English models released 2026-10-20:
`english_18m_2026-10`, `english_7m_2026-10`, `english_2m_2026-10` and their
reward-fine-tuned `*_refl_2026-10` twins (a different architecture from the 100M:
grouped-query attention, `z_proj`, low-rank text table, one-step flow heads,
separate 128-d Mimi decoder). Each pack holds:

| File | Precision / device | Role |
|------|--------------------|------|
| `pocket_state.mlmodelc` | fp16 MLState multifunction, Neural Engine, iOS 18 / macOS 15 | `prefill` (text block into the FlowLM KV state), `generate` (FlowLM step + flow head per frame), `generate_decode` (step + head + fp16 Mimi in one dispatch), plus `write_state` / `decode` where the ANE planner accepts them |
| `mimi_decoder.mlmodelc` | fp32, CPU, iOS 17 | streaming Mimi decoder, 1920 samples per frame; **use this one** |
| `flowlm_step`, `cond_prefill`, `flow_decoder_fused` `.mlmodelc` | fp32 I/O, CPU/GPU, iOS 17 | exact reference path (~1e-6 vs PyTorch), no MLState needed |
| `constants_bin/` | | tokenizer, folded text table, BOS / speaker constants, 27 voice KV snapshots (`<voice>.safetensors`) |
| `arch.json`, `pocket_state.json`, `manifest.json` | | every dimension, threshold and state shape a host needs |

**Recommended host path:** `generate` on the Neural Engine + `mimi_decoder` (fp32) on
the CPU — this is the FluidAudio default (`PocketTtsSmallManager`, `--backend
pocket-small`). The all-ANE `generate_decode` is faster (18m: 2.9 vs 4.4 ms per
80 ms frame) but its fp16 SEANet adds a white noise floor at about −72 dBFS,
audible as hiss in pauses; fp16 rounding, not an ANE fault, and mixed precision is
rejected inside MLState programs. Chunk text at sentence boundaries to ≤ 50 tokens
(upstream `MAX_TOKEN_PER_CHUNK`): on a whole paragraph several voices never emit
EOS, in PyTorch as well. The `phase` input of the Mimi functions is a one-hot of
`frame < n ? frame : n + frame % n` with `n = capacity / 16` (see `pocket_state.json`).

M5 Pro, FluidAudio CLI, 15 s passage: 18m 24x real time (109 MB RSS), 7m 30x
(86 MB), 2m 51x (58 MB); fused fp16 path 34x / 42x / 52x. Whisper transcripts match
the PyTorch output. Voice cloning for the small models is not wired yet (the Mimi
encoder is the 100M English one; only `speaker_proj_weight` differs and ships in the
pack). Conversion code and the full trial log live in the FluidAudio PR #1051 thread.

## Phonon 31m (Gradium): `phonon/phonon-31m-en/`

Core ML pack for [gradium/phonon-31m-en](https://huggingface.co/gradium/phonon-31m-en), Gradium's
31M-parameter English TTS released 2026-10-20. It is the Pocket TTS recipe trained by the Kyutai
founders' spin-off. **This folder is Apache-2.0**, the license of the original weights: see
`phonon/phonon-31m-en/LICENSE` and its [model card](phonon/phonon-31m-en/README.md).

- Same layout and host path as the `v4/` packs: `pocket_state.mlmodelc` (`generate` on the Neural
  Engine) plus the fp32 `mimi_decoder.mlmodelc` on the CPU.
- 103 voices (25 English, 12 characters, French / German / Spanish / Portuguese catalogue voices,
  plus the checkpoint default) as conditioning rows in `voices/`. The model speaks English only.
- Phonon's differences are baked into the graphs or listed in `arch.json`: erf GELU, the
  summed-conditioner input bias, a 251-wide Mimi window, EOS threshold +4.0, and a 25 ms onset fade.
- Matches Gradium's own `ptts` runtime: same frame counts, and identical Whisper transcripts on the
  Neural Engine path. About 20 ms to first audio and ~20x real time on an M5 Pro.

```swift
let tts = PocketTtsSmallManager(model: .phonon31m)
try await tts.initialize()
let wav = try await tts.synthesize(text: "Hello from Phonon.", voice: "en_us_Easton")
```

## Usage

```swift
import FluidAudioTTS

let manager = PocketTtsManager()
try await manager.initialize()
let audio = try await manager.synthesize(text: "Hello, world!")

See https://github.com/FluidInference/FluidAudio for the full Swift framework.

License

CC-BY-4.0, inherited from https://huggingface.co/kyutai/pocket-tts. Attribution to Kyutai is required.

References

- https://huggingface.co/kyutai/pocket-tts
- https://arxiv.org/abs/2410.00037
- https://github.com/FluidInference/FluidAudio

## Changelog

| Date | Files | Change | Issue | mobius PR | FluidAudio PR | HF commit |
|---|---|---|---|---|---|---|
| 2026-11-01 | `v2.1/english_2026-10/*` | Kyutai `english_2026-10` checkpoint pack (v2.1 recipe on pocket-tts 4.0.1; `mimi_decoder` reused from `v2.1/english`, identical codec) | [FluidAudio #1087](https://github.com/FluidInference/FluidAudio/issues/1087) | none | [#1089](https://github.com/FluidInference/FluidAudio/pull/1089) | [5d9bb3c6](https://huggingface.co/FluidInference/pocket-tts-coreml/commit/5d9bb3c643575f9783293670a5181cc51741a097) |
