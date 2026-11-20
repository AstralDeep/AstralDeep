---
license: other
license_name: component-licenses
license_link: https://huggingface.co/Kentucky-Open-Science/Rumble-2-Flash-Astral/tree/main/licenses
base_model:
- AtomicChat/Rumble-2-Flash-GGUF
- LiquidAI/LFM2.5-1.2B-Instruct-GGUF
pipeline_tag: automatic-speech-recognition
tags:
- gguf
- llama.cpp
- ios
- offline
language: [en, ar, zh, fr, de, ja, ko, es]
---

# Rumble 2 Flash for the Astral voice pilot

The v1.1 installer downloads the three Rumble and LFM GGUF files from Kentucky Open Science and the English Piper Lessac medium voice from its Sherpa-ONNX upstream release. Rumble transcribes speech, LFM2.5 1.2B handles local conversation, and Piper speaks English. The complete pack transfers **1,295,943,485 bytes** and occupies **1,309,859,838 bytes** after extraction.

## Download Rumble with its English voice

Run this command on a computer with uv. It installs into `Models/` and works without a Hugging Face account.

```sh
uv run --python 3.11 --script https://huggingface.co/Kentucky-Open-Science/Rumble-2-Flash-Astral/resolve/v1.1/download_models.py --output Models
```

Alternatively, save the [installer](./download_models.py) and run `python3 download_models.py --output Models` with Python 3.11 or newer. Both commands reuse verified files from an earlier installation. A Hub snapshot includes the GGUF files and installer. Run the installer to also fetch and extract the external voice archive.

| Purpose | Installed path | Runtime |
| --- | --- | --- |
| Speech recognition | `rumble/Rumble-2-Flash-AD-Q5_K_M.gguf` | llama.cpp with libmtmd |
| Audio encoder | `rumble/mmproj-Rumble-2-Flash-Q8_0.gguf` | Rumble audio projector |
| Local conversation | `chat-1.2b/LFM2.5-1.2B-Instruct-Q4_K_M.gguf` | llama.cpp |
| English speech | `tts/en_US-lessac-medium.onnx` with tokens and eSpeak NG data | Piper via Sherpa-ONNX, 22,050 Hz mono |

## App integration

Offer a single voice-pack download. Read the v2 manifest from this release, verify the byte count and SHA-256 of each entry in `files` and `archives`, and verify the extracted files under `tts/` before marking the pack ready. The manifest includes the archive source and member paths. The installer handles both lists. The previous v1.0 tag remains available for clients that only read the GGUF file list. [v1.1 manifest](./astral-manifest.json) · [Installed file hashes](./model-hashes.json) · [v1.0 release](https://huggingface.co/Kentucky-Open-Science/Rumble-2-Flash-Astral/tree/v1.0)

The isolated iPhone pilot uses 16 kHz mono input. It bundles this same English voice in the signed app. Arabic, Mandarin Chinese, French, German, Japanese, Korean and Spanish use installed Apple voices. The pilot’s GGUF runtime uses the pinned llama.cpp revision. [llama.cpp source](https://github.com/ggml-org/llama.cpp/tree/8af68d486a9473820d24d00af8f33334ca80124d)

## Sources and licenses

- Reson8 created Rumble 2 Flash. AtomicChat produced the AD-Q5_K_M recognition weights and Q8_0 audio projector distributed here. Both files retain the upstream Apache 2.0 license. [GGUF source](https://huggingface.co/AtomicChat/Rumble-2-Flash-GGUF/tree/d036f2af3cd5229375714d7f1749792cce3c6a88) · [License](./licenses/Rumble-Apache-2.0.txt)
- Liquid AI produced the LFM2.5 1.2B Instruct Q4_K_M chat weights. These retain the upstream LFM Open License, including its commercial-use terms. [GGUF source](https://huggingface.co/LiquidAI/LFM2.5-1.2B-Instruct-GGUF/tree/8ed288026e23958ad9dfa92d53ed773a8eee7125) · [License](./licenses/Liquid-LFM-Open-License.txt)

- The Sherpa-ONNX project hosts the Piper Lessac medium English archive used by the pilot. The installer preserves its original model card. The Piper project identifies Lessac/CSTR as the contact for redistribution rights. Kentucky Open Science supplies download metadata for the external voice. [Upstream archive](https://github.com/k2-fsa/sherpa-onnx/releases/download/tts-models/vits-piper-en_US-lessac-medium.tar.bz2) · [Lessac model card](https://huggingface.co/rhasspy/piper-voices/blob/main/en/en_US/lessac/medium/MODEL_CARD) · [Piper licensing guidance](https://github.com/OHF-Voice/piper1-gpl/blob/main/docs/LICENSING.md) · [Blizzard 2013 data terms](https://www.cstr.ed.ac.uk/projects/blizzard/2013/lessac_blizzard2013/license.html)
