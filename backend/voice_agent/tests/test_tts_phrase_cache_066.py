"""Feature 066: bounded fixed-phrase TTS cache guards."""

from __future__ import annotations

import asyncio
import io
import wave

import pytest

from voice_agent.speech_adapters import (
    KOKORO_SAMPLE_RATE,
    MAX_QUANTUM_SAMPLES,
    SERVER_OWNED_PHRASE_TEXTS,
    SHORT_TERMINAL_PHRASE_TEXTS,
    TTS_CACHE_MAX_ENTRIES,
    TTS_CACHE_MAX_TEXT_CHARS,
    FixedPhraseTTSCache,
    HttpRequest,
    HttpResponse,
    SpeachesTTS,
    SpeechAdapterError,
    SynthesizedAudio,
)
from voice_agent.tests.test_session_start_065 import (
    FakeParticipant,
    FakePublication,
    FakeRoom,
    FakeRtcFactory,
    FakeTrack,
    FakeTts,
    SessionNotice,
    _session,
    _speak,
    _uuid,
    _wait_for,
)

GREETING = "Hi! I'm ready when you are."


def _wav_body(samples: int) -> bytes:
    output = io.BytesIO()
    with wave.open(output, "wb") as writer:
        writer.setnchannels(1)
        writer.setsampwidth(2)
        writer.setframerate(KOKORO_SAMPLE_RATE)
        writer.writeframes(b"\x01\x00" * samples)
    return output.getvalue()


class CountingTransport:
    def __init__(self, samples: int = 960) -> None:
        self.samples = samples
        self.requests: list[HttpRequest] = []

    async def post(self, request: HttpRequest) -> HttpResponse:
        self.requests.append(request)
        return HttpResponse(
            status=200,
            headers={"content-type": "audio/wav"},
            body=_wav_body(self.samples),
        )


class CountingInner:
    def __init__(self) -> None:
        self.calls: list[str] = []

    async def synthesize(
        self, text: str, *, max_duration_samples: int
    ) -> SynthesizedAudio:
        del max_duration_samples
        self.calls.append(text)
        return SynthesizedAudio(
            pcm_s16le=b"\x01\x00" * 480,
            sample_rate=KOKORO_SAMPLE_RATE,
            channels=1,
            sample_width_bytes=2,
            samples=480,
        )


class FailingInner:
    def __init__(self) -> None:
        self.calls = 0

    async def synthesize(
        self, text: str, *, max_duration_samples: int
    ) -> SynthesizedAudio:
        del text, max_duration_samples
        self.calls += 1
        raise SpeechAdapterError("upstream_unavailable")


@pytest.mark.asyncio
async def test_repeat_server_phrase_hits_cache_and_skips_http() -> None:
    transport = CountingTransport()
    tts = FixedPhraseTTSCache(SpeachesTTS(transport=transport, api_key="k"))

    first = await tts.synthesize("On it!", max_duration_samples=96_000)
    second = await tts.synthesize("On it!", max_duration_samples=96_000)

    assert len(transport.requests) == 1
    assert isinstance(second, SynthesizedAudio)
    assert second == first


@pytest.mark.asyncio
async def test_non_phrase_text_bypasses_cache_and_always_synthesizes() -> None:
    transport = CountingTransport()
    tts = FixedPhraseTTSCache(SpeachesTTS(transport=transport, api_key="k"))
    result_text = "Here is what I found about your appointment."

    for _ in range(2):
        await tts.synthesize(result_text, max_duration_samples=96_000)

    assert len(transport.requests) == 2


@pytest.mark.asyncio
async def test_user_content_length_text_bypasses_even_when_listed() -> None:
    long_text = "word " * 60  # 300 chars, beyond the short-phrase regime
    assert len(long_text) > TTS_CACHE_MAX_TEXT_CHARS
    transport = CountingTransport()
    tts = FixedPhraseTTSCache(
        SpeachesTTS(transport=transport, api_key="k"),
        cacheable_texts=frozenset({long_text}),
    )

    for _ in range(2):
        await tts.synthesize(long_text, max_duration_samples=96_000)

    assert len(transport.requests) == 2


@pytest.mark.asyncio
async def test_cache_evicts_oldest_beyond_the_entry_bound() -> None:
    phrases = [f"phrase {index}" for index in range(TTS_CACHE_MAX_ENTRIES + 1)]
    inner = CountingInner()
    tts = FixedPhraseTTSCache(inner, cacheable_texts=frozenset(phrases))

    for phrase in phrases:
        await tts.synthesize(phrase, max_duration_samples=96_000)

    assert len(tts._entries) == TTS_CACHE_MAX_ENTRIES
    # The newest entry is cached; the oldest was evicted and re-synthesizes.
    await tts.synthesize(phrases[-1], max_duration_samples=96_000)
    assert inner.calls.count(phrases[-1]) == 1
    await tts.synthesize(phrases[0], max_duration_samples=96_000)
    assert inner.calls.count(phrases[0]) == 2


@pytest.mark.asyncio
async def test_cache_hit_refreshes_lru_recency() -> None:
    phrases = [f"phrase {index}" for index in range(TTS_CACHE_MAX_ENTRIES + 1)]
    inner = CountingInner()
    tts = FixedPhraseTTSCache(inner, cacheable_texts=frozenset(phrases))

    for phrase in phrases[:TTS_CACHE_MAX_ENTRIES]:
        await tts.synthesize(phrase, max_duration_samples=96_000)
    # Touch the oldest so the next insertion evicts phrase 1 instead.
    await tts.synthesize(phrases[0], max_duration_samples=96_000)
    await tts.synthesize(phrases[-1], max_duration_samples=96_000)

    await tts.synthesize(phrases[0], max_duration_samples=96_000)
    assert inner.calls.count(phrases[0]) == 1
    await tts.synthesize(phrases[1], max_duration_samples=96_000)
    assert inner.calls.count(phrases[1]) == 2


@pytest.mark.asyncio
async def test_failed_synthesis_is_never_cached() -> None:
    inner = FailingInner()
    tts = FixedPhraseTTSCache(inner, cacheable_texts=frozenset({"On it!"}))

    for _ in range(2):
        with pytest.raises(SpeechAdapterError):
            await tts.synthesize("On it!", max_duration_samples=96_000)

    assert inner.calls == 2
    assert len(tts._entries) == 0


@pytest.mark.asyncio
async def test_cache_hit_enforces_ceiling_and_input_validation_like_fresh() -> None:
    transport = CountingTransport(samples=960)
    tts = FixedPhraseTTSCache(SpeachesTTS(transport=transport, api_key="k"))
    await tts.synthesize("On it!", max_duration_samples=96_000)

    with pytest.raises(SpeechAdapterError) as budget:
        await tts.synthesize("On it!", max_duration_samples=480)
    assert budget.value.reason == "audio_budget_exceeded"

    with pytest.raises(SpeechAdapterError) as ceiling:
        await tts.synthesize("On it!", max_duration_samples=0)
    assert ceiling.value.reason == "invalid_sample_ceiling"

    # The entry survives for later valid ceilings and still skips HTTP.
    again = await tts.synthesize("On it!", max_duration_samples=96_000)
    assert again.samples == 960
    assert len(transport.requests) == 1


@pytest.mark.asyncio
async def test_cache_hit_preserves_announcement_and_track_flow() -> None:
    """A hit drives the exact speak -> publish -> finished flow of a miss."""

    publication = FakePublication("TR_mic", track=FakeTrack())
    room = FakeRoom([FakeParticipant("client-a", [publication])])
    factory = FakeRtcFactory(room)
    inner = FakeTts()
    notices: list[SessionNotice] = []
    session = _session(factory, tts=FixedPhraseTTSCache(inner), notices=notices)
    task = asyncio.create_task(session.run())
    await session.wait_started()

    first = _speak(announcement_id=_uuid(40))
    first["text"] = GREETING
    session.deliver(first)
    await _wait_for(
        lambda: sum(item.kind == "speech_finished" for item in notices) == 1
    )
    second = _speak(announcement_id=_uuid(41))
    second["text"] = GREETING
    session.deliver(second)
    await _wait_for(
        lambda: sum(item.kind == "speech_finished" for item in notices) == 2
    )

    # One real synthesis, two full announcement/track publications.
    assert inner.calls == [(GREETING, 96_000)]
    assert len(room.local_participant.published) == 2
    assert len(factory.sources) == 2
    assert not any(item.kind == "speech_failed" for item in notices)

    await session.close("test")
    await task


def test_phrase_vocabulary_matches_coordinator_source_of_truth() -> None:
    """Drift-pin the mirrored closed vocabulary against the coordinator."""

    coordinator = pytest.importorskip(
        "orchestrator.voice_coordinator",
        reason="the isolated worker image ships no orchestrator source",
    )
    assert SERVER_OWNED_PHRASE_TEXTS == frozenset(
        coordinator.APPROVED_PHRASE_TEXT.values()
    )
    # Pre-acceptance rejection projections reuse APPROVED_PHRASE_TEXT keys,
    # so the closed vocabulary above already covers them.
    for _kind, phrase_key in coordinator.PREACCEPTANCE_REJECTION_PHRASES.values():
        assert (
            coordinator.APPROVED_PHRASE_TEXT[phrase_key] in SERVER_OWNED_PHRASE_TEXTS
        )
    short_keys = {
        phrase_key
        for kind in coordinator._SHORT_TERMINAL_KINDS
        for phrase_key in coordinator.APPROVED_PHRASE_KEYS[kind]
    }
    short_keys.update(
        phrase_key
        for kind, phrase_key in coordinator.PREACCEPTANCE_REJECTION_PHRASES.values()
        if kind in coordinator._SHORT_TERMINAL_KINDS
    )
    assert frozenset(SHORT_TERMINAL_PHRASE_TEXTS) == frozenset(
        coordinator.APPROVED_PHRASE_TEXT[key] for key in short_keys
    )


def test_cache_bound_is_documented_and_under_seven_mib() -> None:
    """Worst case: 32 validated 4-second quanta = 6,144,000 bytes."""

    assert TTS_CACHE_MAX_ENTRIES * MAX_QUANTUM_SAMPLES * 2 == 6_144_000
    assert all(
        len(text) <= TTS_CACHE_MAX_TEXT_CHARS for text in SERVER_OWNED_PHRASE_TEXTS
    )
