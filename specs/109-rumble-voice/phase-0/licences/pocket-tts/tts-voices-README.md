# Kyutai TTS voices

Voices available for Kyutai TTS models: Pocket TTS and Kyutai TTS 1.6B.
To find voices you like, go to the ["Files and versions" tab on this site](https://huggingface.co/kyutai/tts-voices/tree/main) or use the interactive widget on the
[Kyutai site](https://kyutai.org/tts).

Do you want more voices?
Help us by [donating your voice](https://unmute.sh/voice-donation)
or open an issue in the [repo of Kyutai TTS 1.6B](https://github.com/kyutai-labs/delayed-streams-modeling/) to suggest permissively-licensed datasets of voices we could add here.

Discussions and PRs are disabled for this repo.
For questions, please use the GitHub issues of the [Kyutai TTS 1.6B repo](https://github.com/kyutai-labs/delayed-streams-modeling).

For some datasets, we also provide enhanced (cleaned) versions of the original recordings, look for `*_enhanced.wav`. Created using [ai-coustics](https://ai-coustics.com/).

For TTS 1.6B, we provide embeddings for both the original and enhanced versions.
The embeddings for the original versions are also cleaned using an internal model based on [Demucs](https://github.com/facebookresearch/demucs),
but the enhancement effect might not be as strong as the `_enhanced.wav` version.

## voice-donations/

Voices of volunteers submitted through our [Unmute Voice Donation Project](https://unmute.sh/voice-donation), licensed as CC0.
The Voice Donation project ran from June 2025 to February 2026 and collected 228 voices that passed verification, out of a total 374 submissions.
A big thank you to all participants ❤️

If you use the voice donations in academic work, please cite:

```bibtex
@misc{unmute_voice_donation,
  title={Unmute Voice Donation Project},
  author={Volhejn, V\'{a}clav},
  year={2025},
  url={https://unmute.sh/voice-donation}
}
```

## vctk/

From the [Voice Cloning Toolkit](https://datashare.ed.ac.uk/handle/10283/3443) dataset,
licensed under the Creative Commons License: Attribution 4.0 International.

Each recording was done with two mics, here we used the `mic1` recordings.
We chose sentence 23 for every speaker because it's generally the longest one to pronounce.

## expresso/

From the [Expresso](https://speechbot.github.io/expresso/) dataset,
licensed under the Creative Commons License: Attribution-NonCommercial 4.0 International.
**Non-commercial use only.**

We select clips from the "conversational" files.
For each pair of "kind" and channel (`ex04-ex01_laughing`, channel 1),
we find one segment with at least 10 consecutive seconds of speech using `VAD_segments.txt`.
We don't include more segments per (kind, channel) to keep the number of voices manageable.

The name of the file indicates how it was selected.
For instance, `ex03-ex02_narration_001_channel1_674s.wav` 
comes from the first audio channel of `audio_48khz/conversational/ex03-ex02/narration/ex03-ex02_narration_001.wav`,
meaning it's speaker `ex03`.
It's a 10-second clip starting at 674 seconds of the original file.

## unmute-prod-website

Voices used at [Unmute.sh](https://unmute.sh/).

Licensing:

- `degaulle-2.wav`: comes from the [Appeal of 18 June](https://en.wikipedia.org/wiki/Appeal_of_18_June), recording [here](https://www.youtube.com/watch?v=ung6UiY3YY4). I don't understand how the license here works exactly, but I think it's safe to assume this recording is in the public domain since it's from 1940.
- `ex04_narration_longform_00001.wav`: comes from the Expresso dataset, so CC-NC
- `p329_022.wav`: comes from VCTK, so CC BY 4.0

The others are our own recordings and you may use them as CC0.

## cml-tts/fr/

French voices selected from the [CML-TTS Dataset](https://openslr.org/146/),
licensed under the Creative Commons License: Attribution 4.0 International.

We also provide enhanced (cleaned) versions of the original recordings, look for `*_enhanced.wav`. Created using [ai-coustics](https://ai-coustics.com/).

## ears/

From the [EARS](https://sp-uhh.github.io/ears_dataset/) dataset,
licensed under the Creative Commons License: Attribution-NonCommercial 4.0 International.
**Non-commercial use only.**

For each of the 107 speakers, we use the middle 10 seconds of the `freeform_speech_01.wav` file.
Additionally, we select two speakers, p003 (female) and p031 (male) and provide speaker embeddings for each of their `emo_*_freeform.wav` files.
This is to allow users to experiment with having a voice of a single speaker with multiple emotions.

## alba-mackenna/

Characters voice-acted by Alba MacKenna:
- Casual: Very casual flavour dialogue.
- Merchant: A seller you'd typically encounter in RPG's etc.
- Announcer: An announcer you'd hear typically in competitive games.
- A Moment By: Private recordings requested by Kinder World for their 'Moment' series.

Released under the CC BY 4.0 licence.

## voice-zero/

A couple of voices from [Voice-Zero](https://github.com/OwenTyme/voice-zero), a curated selection of voices from LibriVox.

We only select voices we use in the default selection of [Pocket TTS](https://kyutai.org/blog/2026-01-13-pocket-tts), for the other voices please refer to the [Voice-Zero repository](https://github.com/OwenTyme/voice-zero) directly.

Released under the CC0 licence.

## Computing voice embeddings (for Kyutai devs)

```python
uv run {root of `moshi` repo}/scripts/tts_make_voice.py \
    --model-root {path to weights dir}/moshi_1e68beda_240/ \
    --loudness-headroom 22 \
    {root of this repo}
```

