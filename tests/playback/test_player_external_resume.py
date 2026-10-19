"""Tests for following a resume made on the device side, and the start position hint.

Music Assistant can be resumed from its own UI. A backend that reports such
resumes (``reports_external_resume``) must take the player back to PLAYING;
DLNA and local backends keep the old behaviour (a paused player ignores a
PLAYING device state).
"""

import asyncio
from unittest.mock import AsyncMock, MagicMock

import pytest

from qobuz_proxy.backends import BackendTrackMetadata, PlaybackState
from qobuz_proxy.backends.base import AudioBackend
from qobuz_proxy.playback import player as player_module
from qobuz_proxy.playback.player import QobuzPlayer


class DeviceBackend(AudioBackend):
    """Backend whose device state the test controls."""

    def __init__(self, external_resume: bool) -> None:
        super().__init__(name="test")
        self._external_resume = external_resume
        self.device_state = PlaybackState.PAUSED
        self.device_position_ms = 0
        self.played: list[BackendTrackMetadata] = []

    @property
    def reports_external_resume(self) -> bool:
        return self._external_resume

    async def play(self, url: str, metadata: BackendTrackMetadata) -> None:
        self.played.append(metadata)

    async def pause(self) -> None: ...
    async def resume(self) -> bool:
        return True

    async def stop(self, *, next_track_id: str | None = None) -> None: ...
    async def seek(self, position_ms: int) -> None: ...
    async def get_position(self) -> int:
        return self.device_position_ms

    async def set_volume(self, level: int) -> None: ...
    async def get_volume(self) -> int:
        return 0

    async def get_state(self) -> PlaybackState:
        return self.device_state

    async def connect(self) -> bool:
        return True

    async def disconnect(self) -> None: ...


async def _coro(value):  # type: ignore[no-untyped-def]
    return value


def _make_player(backend: AudioBackend) -> QobuzPlayer:
    metadata = MagicMock()
    metadata.get_streaming_url = MagicMock(
        side_effect=lambda track_id: _coro(f"http://test/{track_id}")
    )
    metadata.get_metadata = MagicMock(side_effect=lambda track_id: _coro(None))
    metadata.get_track_actual_quality = MagicMock(return_value=None)
    metadata.get_track_format = MagicMock(return_value=(0, 0, 0))
    metadata.log_now_playing_info = MagicMock()
    queue = MagicMock()
    queue.set_current_by_item_id = AsyncMock(return_value=True)
    return QobuzPlayer(queue=queue, metadata_service=metadata, backend=backend)


async def _run_monitor(player: QobuzPlayer, monkeypatch: pytest.MonkeyPatch, ticks: int) -> None:
    """Run the playback monitor loop for a fixed number of iterations."""
    count = 0
    real_sleep = asyncio.sleep

    async def tick(_delay: float) -> None:
        nonlocal count
        count += 1
        if count > ticks:
            player._is_running = False
        await real_sleep(0)

    monkeypatch.setattr(player_module.asyncio, "sleep", tick)
    player._is_running = True
    await player._playback_monitor_loop()


async def _paused_player(backend: DeviceBackend) -> QobuzPlayer:
    player = _make_player(backend)
    await player.play_track(queue_item_id=1, track_id="42")
    await player.pause()
    assert player.state == PlaybackState.PAUSED
    return player


class TestExternalResume:
    async def test_device_resume_is_followed(self, monkeypatch: pytest.MonkeyPatch) -> None:
        backend = DeviceBackend(external_resume=True)
        player = await _paused_player(backend)
        send = AsyncMock()
        monkeypatch.setattr(player, "_send_state_update", send)

        backend.device_state = PlaybackState.PLAYING
        backend.device_position_ms = 42_000
        await _run_monitor(player, monkeypatch, ticks=1)

        assert player.state == PlaybackState.PLAYING
        assert player._position_value_ms == 42_000
        send.assert_awaited()

    async def test_backends_without_the_capability_stay_paused(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """DLNA/local: a PLAYING read while paused is not trusted (unchanged)."""
        backend = DeviceBackend(external_resume=False)
        player = await _paused_player(backend)

        backend.device_state = PlaybackState.PLAYING
        await _run_monitor(player, monkeypatch, ticks=3)

        assert player.state == PlaybackState.PAUSED

    async def test_mock_backend_does_not_trigger_resume(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A MagicMock attribute is truthy; only a real True enables the resume."""
        backend = DeviceBackend(external_resume=False)
        player = await _paused_player(backend)
        monkeypatch.setattr(
            DeviceBackend, "reports_external_resume", property(lambda self: MagicMock())
        )

        backend.device_state = PlaybackState.PLAYING
        await _run_monitor(player, monkeypatch, ticks=2)

        assert player.state == PlaybackState.PAUSED

    async def test_paused_stop_detection_still_works(self, monkeypatch: pytest.MonkeyPatch) -> None:
        backend = DeviceBackend(external_resume=True)
        player = await _paused_player(backend)

        backend.device_state = PlaybackState.STOPPED
        await _run_monitor(player, monkeypatch, ticks=3)

        assert player.state == PlaybackState.STOPPED


class TestStartPositionHint:
    async def test_play_carries_start_position(self) -> None:
        backend = DeviceBackend(external_resume=False)
        player = _make_player(backend)

        await player.play_track(queue_item_id=1, track_id="42", position_ms=61_000)

        assert backend.played[-1].start_position_ms == 61_000

    async def test_play_from_start_has_zero_hint(self) -> None:
        backend = DeviceBackend(external_resume=False)
        player = _make_player(backend)

        await player.play_track(queue_item_id=1, track_id="42")

        assert backend.played[-1].start_position_ms == 0
