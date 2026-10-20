"""Fixtures for every test: private data, fast timers, a fake Music Assistant and the rig."""

from collections.abc import AsyncIterator, Awaitable
from pathlib import Path
from typing import Any, Callable, Optional

import pytest

from qobuz_proxy.backends.music_assistant import backend as backend_module
from qobuz_proxy.backends.music_assistant import connection as connection_module
from qobuz_proxy.backends.music_assistant import party as party_module
from qobuz_proxy.backends.music_assistant.connection import MusicAssistantConnection
from qobuz_proxy.backends.music_assistant.settings import PartySpeaker
from qobuz_proxy.connect import ws_manager
from qobuz_proxy.playback import state_reporter as state_reporter_module
from tests.fake_music_assistant import FakeMusicAssistantServer
from tests.rig import KITCHEN, FakeHomeAssistant, Rig

StartSpeaker = Callable[..., Awaitable[Rig]]


@pytest.fixture(autouse=True)
def _isolated_data_dir(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Keep all the add-on's data in the test's tmp_path, and Home Assistant out of reach."""
    monkeypatch.setattr("qobuz_proxy.app.CACHE_DIR", tmp_path)
    monkeypatch.delenv("SUPERVISOR_TOKEN", raising=False)
    monkeypatch.setattr("qobuz_proxy.auth.credentials.CACHE_DIR", tmp_path)
    monkeypatch.setattr("qobuz_proxy.auth.credentials.CACHE_FILE", tmp_path / "credentials.json")


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
        # The fake moves Music Assistant's position only with advance_time and does not emulate
        # the quirks these windows absorb (stale current item after "next"), so keep them tiny.
        "EXPECT_STATE_SECONDS": 0.5,
        "PROMOTE_SETTLE_SECONDS": 0.1,
        "STALL_CONFIRM_SECONDS": 0.1,
    }.items():
        monkeypatch.setattr(backend_module, name, value)
    monkeypatch.setattr(connection_module, "RECONNECT_DELAYS_SECONDS", (0.02,))
    monkeypatch.setattr(party_module, "PARTY_RELEASE_DELAY_SECONDS", 0.3)
    # A second Qobuz app picking the speaker makes it reconnect to the Qobuz cloud
    monkeypatch.setattr(ws_manager, "INITIAL_RECONNECT_DELAY", 0.02)


@pytest.fixture
def fast_heartbeat(monkeypatch: pytest.MonkeyPatch) -> None:
    """State reports every 0.2 s."""
    monkeypatch.setattr(state_reporter_module, "STATE_UPDATE_INTERVAL_SECONDS", 0.2)


@pytest.fixture
def server() -> FakeMusicAssistantServer:
    """Music Assistant with Qobuz set up and one player, Kitchen."""
    server = FakeMusicAssistantServer()
    server.add_qobuz_provider()
    server.add_player(KITCHEN, "Kitchen")
    return server


@pytest.fixture
def fake_login(server: FakeMusicAssistantServer, monkeypatch: pytest.MonkeyPatch) -> None:
    """Music Assistant logins (username and password) go to the fake server."""
    monkeypatch.setattr(connection_module, "login", server.login)
    monkeypatch.setattr(connection_module, "MusicAssistantClient", server.client_class)


@pytest.fixture
async def start_speaker(
    server: FakeMusicAssistantServer, monkeypatch: pytest.MonkeyPatch
) -> AsyncIterator[StartSpeaker]:
    """Start a player's speaker (Kitchen by default) or a party speaker; pick it in the app."""
    started: list[Rig] = []

    async def start(
        sessions: Optional[dict[str, dict[str, Any]]] = None,
        *,
        player_id: str = KITCHEN,
        name: str = "Kitchen (MA)",
        wait_for_music_assistant: bool = True,
        party: Optional[PartySpeaker] = None,
        home_assistant: Optional[FakeHomeAssistant] = None,
        connection: Optional[MusicAssistantConnection] = None,
    ) -> Rig:
        rig = Rig(
            server,
            monkeypatch,
            player_id=player_id,
            name=name,
            sessions=sessions,
            party=party,
            home_assistant=home_assistant,
            connection=connection,
        )
        started.append(rig)
        await rig.start(wait_for_music_assistant=wait_for_music_assistant)
        await rig.select()
        return rig

    yield start
    for rig in started:
        await rig.close()


@pytest.fixture
async def rig(start_speaker: StartSpeaker) -> Rig:
    """The Kitchen player, exposed and already picked in the Qobuz app."""
    return await start_speaker()
