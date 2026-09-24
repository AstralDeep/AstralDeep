"""Tests for astralprims' Audio primitive: serialization shape and its
create_ui_response envelope.
"""

from astralprims import Audio, create_ui_response


class TestAudioPrimitive:
    def test_serializes_basic_audio(self):
        audio = Audio(
            src="https://example.com/sound.wav",
            contentType="audio/wav",
            label="Test Sound",
        )
        data = audio.to_dict()
        assert data["type"] == "audio"
        assert data["src"] == "https://example.com/sound.wav"
        assert data["contentType"] == "audio/wav"
        assert data["label"] == "Test Sound"

    def test_defaults(self):
        audio = Audio(src="https://example.com/sound.wav")
        data = audio.to_dict()
        assert data["autoplay"] is False
        assert data["loop"] is False
        assert data["showControls"] is True
        assert data.get("label") is None
        assert data.get("description") is None
        assert data.get("contentType") is None

    def test_full_config(self):
        audio = Audio(
            src="data:audio/mpeg;base64,//uQx",
            contentType="audio/mpeg",
            autoplay=True,
            loop=True,
            showControls=False,
            label="Speech",
            description="Generated TTS",
        )
        data = audio.to_dict()
        assert data["autoplay"] is True
        assert data["loop"] is True
        assert data["showControls"] is False
        assert data["description"] == "Generated TTS"

    def test_create_ui_response_with_audio(self):
        audio = Audio(src="https://example.com/sound.wav", label="Mix")
        response = create_ui_response([audio])
        assert "_ui_components" in response
        assert len(response["_ui_components"]) == 1
        assert response["_ui_components"][0]["type"] == "audio"
        assert response["_ui_components"][0]["src"] == "https://example.com/sound.wav"

    def test_create_ui_response_multiple_audio(self):
        a1 = Audio(src="sound1.wav", label="One")
        a2 = Audio(src="sound2.wav", label="Two")
        response = create_ui_response([a1, a2])
        assert len(response["_ui_components"]) == 2
        assert response["_ui_components"][0]["label"] == "One"
        assert response["_ui_components"][1]["label"] == "Two"
