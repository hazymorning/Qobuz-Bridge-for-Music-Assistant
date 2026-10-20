"""One Music Assistant player exposed as a Qobuz Connect device, wired like production.

Real: the Speaker wiring (``Speaker.start`` and ``Speaker._setup_websocket``), the WsManager
connection loop (authenticate, subscribe, join, receive), PlaybackCommandHandler, QueueHandler,
VolumeCommandHandler, StateReporter, QobuzPlayer, QobuzQueue, the shared
MusicAssistantConnection, MusicAssistantBackend and the real music-assistant-client talking to
the in-memory FakeMusicAssistantServer.

Fake: the Qobuz Connect cloud socket (``QobuzCloud`` decodes every frame the renderer sends,
which is exactly what the Qobuz app gets to see), the Qobuz track API (``StubMetadata``: URL and
metadata per track id; a region-blocked track has neither) and mDNS/HTTP discovery.

Time: Music Assistant playback only moves with ``server.advance_time``; the backend's timers are
patched to milliseconds and every wait is a bounded poll (``Rig.wait_for``).
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any, Callable, Optional
from unittest.mock import MagicMock

import pytest
import websockets
from music_assistant_models.enums import PlaybackState as MusicAssistantState

from qobuz_proxy import speaker as speaker_module
from qobuz_proxy.backends.music_assistant.backend import MusicAssistantBackend
from qobuz_proxy.backends.music_assistant.connection import MusicAssistantConnection
from qobuz_proxy.backends.music_assistant.speakers import speaker_uuid
from qobuz_proxy.config import SpeakerConfig
from qobuz_proxy.connect.protocol import DecodedMessage, MessageType
from qobuz_proxy.connect.types import ConnectTokens
from qobuz_proxy.proto import qconnect_payload_pb2 as pb
from qobuz_proxy.speaker import Speaker
from tests.fake_music_assistant import FakeMusicAssistantServer
from tests.fake_qobuz import (
    ALBUM,
    TRACK_S,
    QobuzCloud,
    StubDiscovery,
    StubMetadata,
    album_state,
    make_tokens,
    set_active,
)

PID = "up1"
BACKEND_LOGGER = "qobuz_proxy.backends.music_assistant"


class Rig:
    """One exposed MA player as a Qobuz Connect device, wired like production."""

    def __init__(
        self,
        server: FakeMusicAssistantServer,
        monkeypatch: pytest.MonkeyPatch,
        *,
        player_id: str = PID,
        name: str = "Kitchen (MA)",
        sessions: Optional[dict[str, dict[str, Any]]] = None,
    ) -> None:
        self.server = server
        self.player_id = player_id
        self.name = name
        self.sessions: dict[str, dict[str, Any]] = {} if sessions is None else sessions
        self.cloud = QobuzCloud()
        self.cloud.deliver = self._deliver_later
        self.metadata = StubMetadata()
        self.tokens = make_tokens()
        self.tasks: list[asyncio.Future[Any]] = []
        self.conn: Optional[MusicAssistantConnection] = None
        self.speaker: Optional[Speaker] = None
        self.discovery: Optional[StubDiscovery] = None
        monkeypatch.setattr(websockets, "connect", self.cloud.connect)
        monkeypatch.setattr(speaker_module, "DiscoveryService", self._make_discovery)
        monkeypatch.setattr(speaker_module, "MetadataService", lambda **kw: self.metadata)

    def _make_discovery(self, *args: Any, **kwargs: Any) -> StubDiscovery:
        self.discovery = StubDiscovery(*args, **kwargs)
        return self.discovery

    async def start(self, *, wait_for_ma: bool = True) -> Rig:
        self.conn = MusicAssistantConnection(
            self.server.url, self.server.token, client_factory=self.server.client_factory
        )
        await self.conn.start()
        if wait_for_ma:
            await self.wait_for(lambda: self.conn is not None and self.conn.is_connected, "MA")
        backend = MusicAssistantBackend(
            self.conn,
            self.player_id,
            self.name,
            session=self.sessions.get(self.player_id),
            save_session=self._save_session,
        )
        config = SpeakerConfig(
            name=self.name,
            uuid=speaker_uuid(self.player_id),
            backend_type="music_assistant",
            max_quality=27,
            http_port=8700,
        )
        self.speaker = Speaker(config, MagicMock(), "test-app", backend=backend)
        assert await self.speaker.start()
        return self

    def _save_session(self, session: Optional[dict[str, Any]]) -> None:
        """Keep the backend's session like the music_assistant does (per MA player id)."""
        if session is None:
            self.sessions.pop(self.player_id, None)
        else:
            self.sessions[self.player_id] = session

    async def close(self) -> None:
        if self.speaker is not None:
            await self.speaker.stop()
        if self.conn is not None:
            await self.conn.stop()
        for task in self.tasks:
            task.cancel()
        await self.server.settle()

    async def select(self, tokens: Optional[ConnectTokens] = None) -> None:
        """The user picks this device in the Qobuz app (discovery connect + SET_ACTIVE)."""
        assert self.speaker is not None
        await self.speaker._setup_websocket(tokens or self.tokens)
        await self.wait_for(lambda: self.ws.is_connected, "Qobuz websocket")
        self._track_handlers()
        await self.deliver(set_active(True))

    @property
    def player(self) -> Any:
        assert self.speaker is not None and self.speaker._player is not None
        return self.speaker._player

    @property
    def ws(self) -> Any:
        assert self.speaker is not None and self.speaker._ws_manager is not None
        return self.speaker._ws_manager

    @property
    def backend(self) -> MusicAssistantBackend:
        assert self.speaker is not None and self.speaker._backend is not None
        return self.speaker._backend  # type: ignore[return-value]

    def _track_handlers(self) -> None:
        """Keep the tasks the handlers spawn, so deliver() can wait for them."""
        handlers = self.ws._handlers
        for msg_type, handler in list(handlers.items()):
            if getattr(handler, "_rig_tracked", False):
                continue

            def tracked(mt: int, msg: Any, _h: Callable[..., Any] = handler) -> Any:
                result = _h(mt, msg)
                if isinstance(result, asyncio.Future):
                    self.tasks.append(result)
                return result

            tracked._rig_tracked = True  # type: ignore[attr-defined]
            handlers[msg_type] = tracked

    async def deliver(self, *messages: Any) -> None:
        """The Qobuz cloud sends one batch to this renderer; wait until it is handled."""
        batch = pb.QConnectBatch()
        for message in messages:
            batch.messages.append(message)
        await self.ws._handle_payload(
            DecodedMessage(msg_type=MessageType.PAYLOAD, payload=batch.SerializeToString())
        )
        await self.drain()

    def _deliver_later(self, *messages: Any) -> None:
        self.tasks.append(asyncio.ensure_future(self.deliver(*messages)))

    async def drain(self, timeout: float = 5.0) -> None:
        loop = asyncio.get_running_loop()
        deadline = loop.time() + timeout
        current = asyncio.current_task()
        while True:
            pending = [t for t in self.tasks if not t.done() and t is not current]
            if not pending:
                break
            remaining = deadline - loop.time()
            if remaining <= 0:
                pytest.fail("Qobuz command handling did not finish")
            await asyncio.wait(pending, timeout=remaining)
        for task in [t for t in self.tasks if t.done()]:
            self.tasks.remove(task)
            if not task.cancelled() and task.exception() is not None:
                raise task.exception()  # type: ignore[misc]
        await self.server.settle()

    async def play_album(self, index: int = 0) -> None:
        """The app plays the album from track ``index``; waits until MA plays it."""
        await self.deliver(album_state(index))
        await self.wait_for(
            lambda: (
                self.queue_track() == ALBUM[index]
                and self.queue_state() == MusicAssistantState.PLAYING
            ),
            f"MA playing {ALBUM[index]}",
        )

    async def play_to_near_end(self, left_s: float = 1.0) -> None:
        """MA plays on until ``left_s`` before the track's end, and the player has seen it.

        MA sends no position ticks: the player's 0.5 s monitor reads the position, so the
        backend only knows the track got near its end once a poll ran.
        """
        elapsed = self.server.queue(self.player_id).corrected_elapsed_time
        self.server.advance_time(self.player_id, TRACK_S - left_s - elapsed)
        await self.wait_for(
            lambda: self.player._position_value_ms >= (TRACK_S - left_s - 1) * 1000,
            "the player saw the position near the end",
        )

    async def finish_track(self) -> None:
        """The current track plays to its natural end on the speaker."""
        await self.play_to_near_end()
        self.server.finish_current_track(self.player_id)

    async def wait_armed(self, track_id: str) -> None:
        await self.wait_for(
            lambda: (
                self.player._gapless_armed
                and self.player._pending_next_track["trackId"] == track_id
                and track_id in self.server.track_ids(self.player_id)
            ),
            f"next track {track_id} queued in MA",
        )

    def app(self) -> tuple[int, int]:
        """(playing state, current queue item id) of the last report the app got."""
        assert self.cloud.reports, "the app got no state report"
        report = self.cloud.reports[-1]
        return report.playingState, report.currentQueueItemId

    def app_position(self) -> int:
        return int(self.cloud.reports[-1].currentPosition.value)

    def queue_track(self) -> Optional[str]:
        queue = self.server.queue(self.player_id)
        if queue.current_index is None:
            return None
        return self.server.track_ids(self.player_id)[queue.current_index]

    def queue_state(self) -> MusicAssistantState:
        return self.server.queue(self.player_id).state

    def plays(self) -> list[tuple[str, list[str]]]:
        """(option, media uris) of every play_media the proxy sent."""
        result = []
        for args in self.server.calls("player_queues/play_media"):
            media = args.get("media")
            result.append((str(args.get("option")), media if isinstance(media, list) else [media]))
        return result

    def writes(self) -> list[str]:
        return [name for name, _ in self.server.writes]

    async def wait_for(self, cond: Callable[[], Any], what: str, timeout: float = 3.0) -> None:
        loop = asyncio.get_running_loop()
        deadline = loop.time() + timeout
        while True:
            await self.server.settle()
            if cond():
                return
            if loop.time() > deadline:
                pytest.fail(f"timed out waiting for {what}")
            await asyncio.sleep(0.005)

    async def idle(self, seconds: float = 0.3) -> None:
        """Let real time pass (timers, the player's 0.5 s monitor) with MA events flowing."""
        loop = asyncio.get_running_loop()
        deadline = loop.time() + seconds
        while loop.time() < deadline:
            await self.server.settle()
            await asyncio.sleep(0.01)
        await self.drain()


def make_server() -> FakeMusicAssistantServer:
    server = FakeMusicAssistantServer()
    server.add_qobuz_provider()
    server.add_player(PID, "Kitchen")
    return server


def assert_no_backend_errors(caplog: pytest.LogCaptureFixture) -> None:
    errors = [
        r
        for r in caplog.records
        if r.name.startswith(BACKEND_LOGGER) and r.levelno >= logging.ERROR
    ]
    assert not errors, [r.getMessage() for r in errors]
