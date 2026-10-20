"""The Music Assistant backend rig shared by ``test_ma_backend.py`` and ``test_ma_events.py``.

The backend talks to a real MusicAssistantConnection whose client is the real
``music-assistant-client`` wired to the in-memory fake server; the Player is replaced
by mocks on the backend callbacks. The backend's timing constants are patched to tiny
values (``FAST_TIMING``) and every wait is a bounded poll.

The MA packages are imported lazily (Python 3.13+ only), so the other backend tests in
this folder keep running without them.
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any, Callable, Optional
from unittest.mock import AsyncMock, MagicMock

import pytest

from qobuz_proxy.backends.types import BackendTrackMetadata, PlaybackState

if TYPE_CHECKING:
    from music_assistant_models.enums import PlaybackState as MusicAssistantState

    from qobuz_proxy.backends.music_assistant.backend import MusicAssistantBackend
    from qobuz_proxy.backends.music_assistant.connection import MusicAssistantConnection
    from tests.fake_music_assistant import FakeMusicAssistantServer

QID = "up1"
NAME = "Kitchen (MA)"
STREAM_URL = "https://streaming.qobuz.example/file.flac"  # ignored by the backend

FAST_TIMING: dict[str, float] = {
    "START_GRACE_SECONDS": 0.05,
    "IDLE_SETTLE_SECONDS": 0.05,
    "PLAY_ACTION_WINDOW_SECONDS": 0.1,
    "DISCONNECT_GRACE_SECONDS": 0.1,
    "VOLUME_DEBOUNCE_SECONDS": 0.02,
    "VOLUME_SETTLE_SECONDS": 0.05,
    "JUMP_WAIT_SECONDS": 0.05,
    "NEXT_ARM_DELAY_SECONDS": 0.0,
    "NEXT_RETRY_COOLDOWN_SECONDS": 0.05,
    "EXPECT_STATE_SECONDS": 0.1,
    "STALL_CONFIRM_SECONDS": 0.05,
    "PROMOTE_SETTLE_SECONDS": 0.1,
}

NOTHING_FIRED = {"track_ended": 0, "next_started": 0, "interrupted": 0, "external": 0}


def meta(track_id: str, start_ms: int = 0, duration_ms: int = 200_000) -> BackendTrackMetadata:
    return BackendTrackMetadata(
        track_id=track_id,
        title=f"Song {track_id}",
        artist="Artist",
        album="Album",
        duration_ms=duration_ms,
        start_position_ms=start_ms,
    )


@dataclass
class Callbacks:
    """The Player side: one mock per backend callback."""

    state: MagicMock = field(default_factory=MagicMock)
    position: MagicMock = field(default_factory=MagicMock)
    track_ended: MagicMock = field(default_factory=MagicMock)
    next_started: MagicMock = field(default_factory=MagicMock)
    interrupted: MagicMock = field(default_factory=MagicMock)
    volume: MagicMock = field(default_factory=MagicMock)
    external: AsyncMock = field(default_factory=AsyncMock)

    def wire(self, backend: MusicAssistantBackend) -> Callbacks:
        backend.on_state_change(self.state)
        backend.on_position_update(self.position)
        backend.on_track_ended(self.track_ended)
        backend.on_next_track_started(self.next_started)
        backend.on_playback_interrupted(self.interrupted)
        backend.on_volume_change(self.volume)
        backend.on_external_playback(self.external)
        return self

    def endings(self) -> dict[str, int]:
        """How often each 'the track is over' callback fired."""
        return {
            "track_ended": self.track_ended.call_count,
            "next_started": self.next_started.call_count,
            "interrupted": self.interrupted.call_count,
            "external": self.external.await_count,
        }


class Rig:
    """Fake MA server + real connection + one backend with mocked Player callbacks."""

    def __init__(self, server: FakeMusicAssistantServer) -> None:
        self.server = server
        self.connections: list[MusicAssistantConnection] = []
        self.backends: list[MusicAssistantBackend] = []
        self.conn = self.new_connection()
        self.backend, self.cb = self.make_backend()

    def new_connection(self) -> MusicAssistantConnection:
        """A connection of its own (another bridge process); started by ``start()``."""
        from qobuz_proxy.backends.music_assistant.connection import MusicAssistantConnection

        conn = MusicAssistantConnection(
            self.server.url, self.server.token, client_factory=self.server.client_factory
        )
        self.connections.append(conn)
        return conn

    def make_backend(
        self,
        player_id: str = QID,
        name: str = NAME,
        session: Optional[dict[str, Any]] = None,
        save_session: Optional[Callable[[Optional[dict[str, Any]]], None]] = None,
        conn: Optional[MusicAssistantConnection] = None,
    ) -> tuple[MusicAssistantBackend, Callbacks]:
        from qobuz_proxy.backends.music_assistant.backend import MusicAssistantBackend

        backend = MusicAssistantBackend(
            conn or self.conn,
            player_id,
            name,
            session=session,
            save_session=save_session or (lambda s: None),
        )
        self.backends.append(backend)
        return backend, Callbacks().wire(backend)

    async def stop(self) -> None:
        for backend in self.backends:
            await backend.disconnect()
        for conn in self.connections:
            await conn.stop()

    async def settle(self) -> None:
        await self.server.settle()

    async def wait(self, cond: Callable[[], bool], timeout: float = 2.0, what: str = "") -> None:
        """Poll ``cond`` every 5 ms (letting MA frames through) until it holds."""
        loop = asyncio.get_running_loop()
        deadline = loop.time() + timeout
        while True:
            await self.server.settle()
            if cond():
                return
            if loop.time() > deadline:
                raise AssertionError(f"timed out waiting for {what or cond}")
            await asyncio.sleep(0.005)

    async def idle(self, seconds: float) -> None:
        """Let real time pass (patched timers fire), then deliver MA frames."""
        await asyncio.sleep(seconds)
        await self.server.settle()

    async def lose_ma(self) -> None:
        """MA becomes unreachable and the connection notices."""
        self.server.set_reachable(False, drop=True)
        await self.wait(lambda: not self.conn.is_connected, what="connection lost")

    async def play(
        self, track_id: str, start_ms: int = 0, backend: Optional[MusicAssistantBackend] = None
    ) -> None:
        await (backend or self.backend).play(STREAM_URL, meta(track_id, start_ms))
        await self.settle()

    async def play_for(
        self, track_id: str, seconds: float, backend: Optional[MusicAssistantBackend] = None
    ) -> None:
        """Play a track and let it run ``seconds``, polled like the Player's monitor loop."""
        await self.play(track_id, backend=backend)
        await self.idle(FAST_TIMING["START_GRACE_SECONDS"] + 0.01)
        self.server.advance_time(QID, seconds)
        await self.poll(backend)

    async def arm(
        self,
        track_id: str,
        queue_item_id: int = 0,
        backend: Optional[MusicAssistantBackend] = None,
    ) -> bool:
        b = backend or self.backend
        ok = await b.set_next_track(STREAM_URL, meta(track_id), queue_item_id)
        await self.settle()
        return ok

    async def poll(self, backend: Optional[MusicAssistantBackend] = None) -> PlaybackState:
        """One round of the Player's monitor loop (state + position)."""
        b = backend or self.backend
        state = await b.get_state()
        await b.get_position()
        return state

    def queue(self) -> Any:
        return self.server.queue(QID)

    def queue_state(self) -> MusicAssistantState:
        return self.server.queue(QID).state

    def queue_track(self) -> Optional[str]:
        item = self.server.queue(QID).current_item
        return item.media_item.item_id if item is not None and item.media_item else None

    def item_id_at(self, index: int) -> str:
        return self.server.item_ids(QID)[index]

    def calls(self, command: str) -> list[dict[str, Any]]:
        return self.server.calls(command)

    def write_names(self) -> list[str]:
        return [name for name, _ in self.server.writes]


@pytest.fixture
def timing(monkeypatch: pytest.MonkeyPatch) -> Callable[..., None]:
    """Patch the MA timing constants to FAST_TIMING; returns a function that overrides some."""
    pytest.importorskip("music_assistant_client")
    from qobuz_proxy.backends.music_assistant import backend, connection

    def apply(**values: float) -> None:
        for name, value in values.items():
            monkeypatch.setattr(backend, name, value)

    apply(**FAST_TIMING)
    monkeypatch.setattr(connection, "RECONNECT_DELAYS_SECONDS", (0.01,))
    return apply


@pytest.fixture
async def rig(timing: Callable[..., None]) -> AsyncIterator[Rig]:
    """A rig whose connection is up and whose backend is connected."""
    from tests.fake_music_assistant import FakeMusicAssistantServer

    server = FakeMusicAssistantServer()
    server.add_qobuz_provider()
    server.add_player(QID, "Kitchen")
    r = Rig(server)
    try:
        await r.conn.start()
        await r.wait(lambda: r.conn.is_connected, what="MA connection")
        await r.backend.connect()
        yield r
    finally:
        await r.stop()
