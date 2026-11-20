# Phonon 31m (Gradium) → Core ML

Core ML conversion of [gradium/phonon-31m-en](https://huggingface.co/gradium/phonon-31m-en), Gradium's 31M-parameter
English TTS model (released 2026-10-20). It builds on Kyutai's Pocket TTS and the Mimi codec.

**License: Apache-2.0**, the license of the original weights, tokenizer and voices (see `LICENSE`).
This folder holds converted weights only; the conversion does not change what the model is. The rest of this
repository has its own license.

## Contents

| File | What it is |
|---|---|
| `pocket_state.mlmodelc` / `.mlpackage` | fp16 MLState multifunction package, iOS 18 / macOS 15+, Neural Engine: `prefill`, `generate` (flow-LM step + one-step flow head), `write_state`, and `decode` / `generate_decode` with an fp16 Mimi |
| `mimi_decoder.mlmodelc` / `.mlpackage` | fp32 streaming Mimi decoder for the CPU. **Use this one:** the fp16 Mimi adds near-white noise at about −80 dBFS in pauses |
| `flowlm_step`, `cond_prefill`, `flow_decoder_fused` | fp32 I/O reference models (iOS 17+) |
| `constants_bin/` | tokenizer (`tokenizer.model` and `tokenizer.json` give the same ids), text embedding table, BOS latent, speaker projection, and KV-cache snapshots for `default`, `en_us_Easton`, `en_us_Zoey`, `en_gb_Maeve` |
| `voices/` | all 103 voices (Gradium's catalogue plus the checkpoint default) as flow-LM conditioning rows `emb [1, T, 512]`, prefilled before the text. Names follow `ptts`: `<locale>_<Name>`. `voices_catalog.jsonl` has descriptions |
| `arch.json`, `pocket_state.json`, `manifest.json`, `constants/` | dimensions, Mimi state layout and the full host contract |

## Host contract (summary; `manifest.json` has all of it)

- Chunk text at ≤ 50 tokens on sentence boundaries. Each chunk restarts from the voice state and a zeroed Mimi state.
- Normalize text first (numbers, currency, emails, URLs, symbols) as Gradium's `ptts` does.
- Stop when the EOS logit exceeds +4.0, then emit 1 more frame (3 for texts of ≤ 4 words).
- Noise std is `sqrt(temperature)`, default temperature 0.8.
- Apply a 25 ms raised-cosine fade-in at each chunk start. The decoder's cold-start click is −40 dBFS without it and −72 dBFS with it.

## Verification

Against Gradium's own runtime (`pip install ptts`, f32, temperature 0):

- Same frame counts.
- Each flow-LM step matches to 1e-6 when teacher-forced.
- The fp32 Mimi matches to −134 dBFS on identical latents.
- Whisper large-v3-turbo transcripts are identical for `ptts`, the fp32 path and the Neural Engine path.

On an M5 Pro, `generate` on the ANE plus the fp32 Mimi on the CPU runs at 3.25 ms per 80 ms frame (24.6× real time).

Swift host: [FluidAudio](https://github.com/FluidInference/FluidAudio) `PocketTtsSmallManager(model: .phonon31m)`.
