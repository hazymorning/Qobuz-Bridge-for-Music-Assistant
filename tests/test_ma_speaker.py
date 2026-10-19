"""Speaker wiring specific to Music Assistant devices."""

import asyncio
from unittest.mock import AsyncMock, MagicMock

import pytest

from qobuz_proxy.config import SpeakerConfig
from qobuz_proxy.playback.volume_handler import (
    MSG_TYPE_SRVR_CTRL_VOLUME_CHANGED,
    MSG_TYPE_SRVR_RNDR_SET_VOLUME,
)
from qobuz_proxy.speaker import Speaker


def _speaker(backend_type: str, active: bool) -> tuple[Speaker, AsyncMock]:
    config = SpeakerConfig(name="Kitchen", backend_type=backend_type)
    speaker = Speaker(config=config, api_client=MagicMock(), app_id="app")
    handler = MagicMock()
    handler.handle_message = AsyncMock()
    speaker._volume_handler = handler
    ws = MagicMock()
    ws.is_renderer_active = active
    speaker._ws_manager = ws
    return speaker, handler.handle_message


@pytest.mark.parametrize(
    ("backend_type", "active", "applied"),
    [
        ("music_assistant", True, True),
        ("music_assistant", False, False),  # another device's volume broadcast
        ("dlna", False, True),  # upstream behaviour unchanged
    ],
)
async def test_volume_broadcast_only_reaches_the_active_ma_device(
    backend_type: str, active: bool, applied: bool
) -> None:
    speaker, handle = _speaker(backend_type, active)

    speaker._dispatch_volume(MSG_TYPE_SRVR_CTRL_VOLUME_CHANGED, object())
    await asyncio.sleep(0)

    assert handle.await_count == (1 if applied else 0)


async def test_direct_volume_command_always_applies() -> None:
    speaker, handle = _speaker("music_assistant", active=False)

    speaker._dispatch_volume(MSG_TYPE_SRVR_RNDR_SET_VOLUME, object())
    await asyncio.sleep(0)

    handle.assert_awaited_once()


def test_device_volume_change_is_only_reported_while_active() -> None:
    speaker, _ = _speaker("music_assistant", active=False)
    player = MagicMock()
    player.broadcast_current_volume = AsyncMock()
    speaker._player = player

    speaker._on_device_volume_change(40)

    player.broadcast_current_volume.assert_not_called()
