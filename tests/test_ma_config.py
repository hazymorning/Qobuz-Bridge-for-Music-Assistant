"""Tests for the music_assistant speaker type in the config layer."""

import pytest

from qobuz_proxy.backends.factory import BackendFactory, BackendNotFoundError
from qobuz_proxy.config import (
    Config,
    ConfigError,
    SpeakerConfig,
    _validate_speakers,
    build_speaker_configs,
    dict_to_config,
    speaker_config_to_dict,
    validate_config,
)


class TestYamlSpeakers:
    def test_music_assistant_speaker_is_parsed(self) -> None:
        config = Config()
        speakers = build_speaker_configs(
            config,
            [{"name": "Kitchen", "backend": "music_assistant", "ma_player_id": "up1234"}],
        )

        assert len(speakers) == 1
        assert speakers[0].backend_type == "music_assistant"
        assert speakers[0].ma_player_id == "up1234"
        # No audio proxy port for MA speakers
        assert speakers[0].proxy_port == 0

    def test_music_assistant_speaker_needs_a_player_id(self) -> None:
        with pytest.raises(ConfigError, match="Music Assistant player id is required"):
            _validate_speakers([SpeakerConfig(name="Kitchen", backend_type="music_assistant")])

    def test_unknown_backend_is_still_rejected(self) -> None:
        with pytest.raises(ConfigError, match="unknown backend type 'airplay'"):
            _validate_speakers([SpeakerConfig(name="Kitchen", backend_type="airplay")])

    def test_round_trip_keeps_player_id(self) -> None:
        sc = SpeakerConfig(name="Kitchen", backend_type="music_assistant", ma_player_id="up1234")
        d = speaker_config_to_dict(sc)

        assert d["backend"] == "music_assistant"
        assert d["ma_player_id"] == "up1234"
        assert "dlna_ip" not in d
        assert "audio_device" not in d

        parsed = build_speaker_configs(Config(), [d])
        assert parsed[0].ma_player_id == "up1234"


class TestFlatConfig:
    def test_flat_music_assistant_config_builds_a_speaker(self) -> None:
        config = dict_to_config(
            {
                "device": {"name": "Living room"},
                "backend": {"type": "music_assistant", "music_assistant": {"player_id": "ma_1"}},
            }
        )
        validate_config(config)
        speakers = build_speaker_configs(config)

        assert [s.ma_player_id for s in speakers] == ["ma_1"]
        assert speakers[0].backend_type == "music_assistant"

    def test_flat_music_assistant_config_without_player_id_is_invalid(self) -> None:
        config = dict_to_config({"backend": {"type": "music_assistant"}})
        with pytest.raises(ConfigError, match="Music Assistant player id is required"):
            validate_config(config)


class TestFactory:
    async def test_without_a_connection_the_backend_is_not_created(self) -> None:
        with pytest.raises(BackendNotFoundError, match="Music Assistant is not set up"):
            await BackendFactory.create_music_assistant(connection=None, player_id="ma_1")
