"""Fixtures for every test: private settings, fast timers, a fake Music Assistant and the rig."""

from collections.abc import AsyncIterator
from pathlib import Path

import pytest

from qobuz_proxy.backends.music_assistant import backend as backend_module
from qobuz_proxy.backends.music_assistant import connection as connection_module
from qobuz_proxy.connect import ws_manager
from qobuz_proxy.playback import state_reporter as state_reporter_module
from tests.fake_music_assistant import FakeMusicAssistantServer
from tests.rig import Rig, make_server


@pytest.fixture(autouse=True)
def _isolated_music_assistant_settings(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Keep the app's Music Assistant settings out of the developer's data dir."""
    monkeypatch.setattr("qobuz_proxy.app.CACHE_DIR", tmp_path)


@pytest.fixture(autouse=True)
def fast_timers(monkeypatch: pytest.MonkeyPatch) -> None:
    """Shrink the backend's real-time windows so scenarios run in milliseconds."""
    for name, value in {
        "START_GRACE_SECONDS": 0.15,
        "IDLE_SETTLE_SECONDS": 0.05,
        "PLAY_ACTION_WINDOW_SECONDS": 0.1,
        "DISCONNECT_GRACE_SECONDS": 0.3,
        "VOLUME_DEBOUNCE_SECONDS": 0.01,
        "VOLUME_SETTLE_SECONDS": 0.05,
        "JUMP_WAIT_SECONDS": 0.2,
        "NEXT_ARM_DELAY_SECONDS": 0.0,
        "NEXT_RETRY_COOLDOWN_SECONDS": 0.05,
        # The fake moves MA's position only with advance_time and does not emulate the
        # quirks these windows absorb (stale current item after "next"), so keep them tiny.
        "EXPECT_STATE_SECONDS": 0.5,
        "PROMOTE_SETTLE_SECONDS": 0.1,
        "STALL_CONFIRM_SECONDS": 0.1,
    }.items():
        monkeypatch.setattr(backend_module, name, value)
    monkeypatch.setattr(connection_module, "RECONNECT_DELAYS_SECONDS", (0.02,))
    # A second Qobuz app picking the speaker makes it reconnect to the Qobuz cloud
    monkeypatch.setattr(ws_manager, "INITIAL_RECONNECT_DELAY", 0.02)


@pytest.fixture
def fast_heartbeat(monkeypatch: pytest.MonkeyPatch) -> None:
    """State reports every 0.2 s (request it before ``rig``: the reporter reads it per sleep)."""
    monkeypatch.setattr(state_reporter_module, "STATE_UPDATE_INTERVAL_SECONDS", 0.2)


@pytest.fixture
def server() -> FakeMusicAssistantServer:
    return make_server()


@pytest.fixture
def fake_login(server: FakeMusicAssistantServer, monkeypatch: pytest.MonkeyPatch) -> None:
    """Music Assistant logins (username and password) go to the fake server."""
    monkeypatch.setattr(connection_module, "login", server.login)
    monkeypatch.setattr(connection_module, "MusicAssistantClient", server.client_class)


@pytest.fixture
async def rig(
    server: FakeMusicAssistantServer, monkeypatch: pytest.MonkeyPatch
) -> AsyncIterator[Rig]:
    """The Kitchen player, exposed and already picked in the Qobuz app."""
    rig = await Rig(server, monkeypatch).start()
    await rig.select()
    try:
        yield rig
    finally:
        await rig.close()
