"""One Music Assistant player (or party speaker) exposed as a Qobuz Connect device, wired like
production.

Real: the Speaker wiring (``Speaker.start`` and ``Speaker._setup_websocket``), the WsManager
connection loop (authenticate, subscribe, join), PlaybackCommandHandler, QueueHandler,
VolumeCommandHandler, StateReporter, QobuzPlayer, QobuzQueue, the shared
MusicAssistantConnection, MusicAssistantBackend and the real music-assistant-client talking to
the in-memory FakeMusicAssistantServer.

Fake: the Qobuz Connect cloud (``QobuzCloud`` decodes every frame the renderer sends, which is
exactly what the Qobuz app gets to see; the app's frames are injected at
``WsManager._handle_payload``), the Qobuz track API (``StubMetadata``: URL and metadata per track
id; a region-blocked track has neither), mDNS/HTTP discovery and Home Assistant
(``FakeHomeAssistant``).

Time: Music Assistant playback only moves with ``server.advance_time``; the backend's timers are
patched to milliseconds and every wait is a bounded poll (``Rig.wait_for``).
"""

from __future__ import annotations

import asyncio
import inspect
import logging
from itertools import groupby
from typing import Any, Callable, Optional
from unittest.mock import MagicMock

import pytest
import websockets
from music_assistant_models.enums import PlaybackState as MusicAssistantState

from qobuz_proxy import speaker as speaker_module
from qobuz_proxy.backends.music_assistant.backend import MusicAssistantBackend
from qobuz_proxy.backends.music_assistant.connection import MusicAssistantConnection
from qobuz_proxy.backends.music_assistant.home_assistant import (
    NOT_AVAILABLE,
    HomeAssistantError,
    MediaPlayer,
)
from qobuz_proxy.backends.music_assistant.party import Party, PartyBackend
from qobuz_proxy.backends.music_assistant.settings import PartySpeaker
from qobuz_proxy.backends.music_assistant.speakers import speaker_uuid
from qobuz_proxy.config import SpeakerConfig
from qobuz_proxy.connect.protocol import DecodedMessage, MessageType
from qobuz_proxy.connect.types import ConnectTokens
from qobuz_proxy.proto import qconnect_payload_pb2 as pb
from qobuz_proxy.speaker import Speaker
from tests.fake_music_assistant import TRACK_DURATION, FakeMusicAssistantServer
from tests.fake_qobuz import (
    ALBUM,
    QobuzCloud,
    StubDiscovery,
    StubMetadata,
    album_state,
    make_tokens,
    set_active,
)

KITCHEN = "up1"  # the player id of Kitchen, the one player every test starts with
BACKEND_LOGGER = "qobuz_proxy.backends.music_assistant"


async def wait_for(
    condition: Callable[[], Any],
    what: str,
    *,
    server: Optional[FakeMusicAssistantServer] = None,
    timeout: float = 3.0,
) -> None:
    """Poll ``condition`` (plain or async) until it holds, letting Music Assistant's events in."""
    loop = asyncio.get_running_loop()
    deadline = loop.time() + timeout
    while True:
        if server is not None:
            await server.settle()
        result = condition()
        if inspect.isawaitable(result):
            result = await result
        if result:
            return
        if loop.time() > deadline:
            pytest.fail(f"timed out waiting for {what}")
        await asyncio.sleep(0.005)


async def idle(server: FakeMusicAssistantServer, seconds: float) -> None:
    """Let real time pass with Music Assistant's events flowing."""
    loop = asyncio.get_running_loop()
    deadline = loop.time() + seconds
    while loop.time() < deadline:
        await server.settle()
        await asyncio.sleep(0.01)


class FakeHomeAssistant:
    """Home Assistant's media players as the add-on reaches them, in-process.

    ``players`` is what ``media_players()`` returns; ``join`` and ``unjoin`` move the entities
    into and out of the main player's ``group_members`` like a speaker integration does.
    ``calls`` logs them as ``("join", main, *members)`` and ``("unjoin", entity_id)``; a
    ``failure`` message makes every call fail with it.
    """

    def __init__(self, *players: MediaPlayer, available: bool = True) -> None:
        self.available = available
        self.players = {p.entity_id: p for p in players}
        self.calls: list[tuple[str, ...]] = []
        self.failure = ""

    async def media_players(self) -> list[MediaPlayer]:
        self._check()
        return list(self.players.values())

    async def join(self, main_entity_id: str, member_ids: list[str]) -> None:
        self._check()
        self.calls.append(("join", main_entity_id, *member_ids))
        main = self.players[main_entity_id]
        members = dict.fromkeys([main_entity_id, *main.group_members, *member_ids])
        self.players[main_entity_id] = main._replace(group_members=list(members))

    async def unjoin(self, entity_id: str) -> None:
        self._check()
        self.calls.append(("unjoin", entity_id))
        for player in list(self.players.values()):
            if entity_id in player.group_members and entity_id != player.entity_id:
                members = [m for m in player.group_members if m != entity_id]
                self.players[player.entity_id] = player._replace(
                    group_members=members if len(members) > 1 else []
                )

    def _check(self) -> None:
        if not self.available:
            raise HomeAssistantError(NOT_AVAILABLE)
        if self.failure:
            raise HomeAssistantError(self.failure)


class Rig:
    """One exposed Music Assistant player as a Qobuz Connect device, wired like production.

    With ``party`` it is that party speaker instead, playing on its main player.
    """

    def __init__(
        self,
        server: FakeMusicAssistantServer,
        monkeypatch: pytest.MonkeyPatch,
        *,
        player_id: str = KITCHEN,
        name: str = "Kitchen (MA)",
        sessions: Optional[dict[str, dict[str, Any]]] = None,
        party: Optional[PartySpeaker] = None,
        home_assistant: Optional[FakeHomeAssistant] = None,
    ) -> None:
        self.server = server
        self.player_id = party.main_player_id if party is not None else player_id
        self.name = name
        self.party_speaker = party
        self.home_assistant = home_assistant or FakeHomeAssistant(available=False)
        self.sessions: dict[str, dict[str, Any]] = {} if sessions is None else sessions
        self.cloud = QobuzCloud()
        self.cloud.deliver = self._deliver_later
        self.metadata = StubMetadata()
        self.tokens = make_tokens()
        self.tasks: list[asyncio.Future[Any]] = []
        self.connection: Optional[MusicAssistantConnection] = None
        self.speaker: Optional[Speaker] = None
        monkeypatch.setattr(websockets, "connect", self.cloud.connect)
        monkeypatch.setattr(speaker_module, "DiscoveryService", StubDiscovery)
        monkeypatch.setattr(speaker_module, "MetadataService", lambda **kwargs: self.metadata)

    async def start(self, *, wait_for_music_assistant: bool = True) -> Rig:
        connection = self.connection = MusicAssistantConnection(
            self.server.url, self.server.token, client_factory=self.server.client_factory
        )
        await connection.start()
        if wait_for_music_assistant:
            await self.wait_for(lambda: connection.is_connected, "Music Assistant connected")
        session = self.sessions.get(self.player_id)
        backend: MusicAssistantBackend
        if self.party_speaker is None:
            backend = MusicAssistantBackend(
                connection,
                self.player_id,
                self.name,
                session=session,
                save_session=self._save_session,
            )
        else:
            party = Party(self.party_speaker, connection, self.home_assistant, self.name)
            backend = PartyBackend(
                connection, party, self.name, session=session, save_session=self._save_session
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
        """Keep the backend's session like MusicAssistantSpeakers does (per player id)."""
        if session is None:
            self.sessions.pop(self.player_id, None)
        else:
            self.sessions[self.player_id] = session

    async def close(self) -> None:
        if self.speaker is not None:
            await self.speaker.stop()
        if self.connection is not None:
            await self.connection.stop()
        for task in self.tasks:
            task.cancel()
        await self.server.settle()

    async def select(self, tokens: Optional[ConnectTokens] = None) -> None:
        """The user picks this device in the Qobuz app (discovery connect + SET_ACTIVE)."""
        assert self.speaker is not None
        await self.speaker._setup_websocket(tokens or self.tokens)
        await self.wait_for(lambda: self.ws.is_connected, "the Qobuz websocket")
        self._track_handlers()
        await self.deliver(set_active(True))

    @property
    def player(self) -> Any:
        assert self.speaker is not None and self.speaker._player is not None
        return self.speaker._player

    @property
    def party(self) -> Party:
        assert self.speaker is not None and isinstance(self.speaker._backend, PartyBackend)
        return self.speaker._backend.party

    @property
    def ws(self) -> Any:
        assert self.speaker is not None and self.speaker._ws_manager is not None
        return self.speaker._ws_manager

    def _track_handlers(self) -> None:
        """Keep the tasks the handlers spawn, so deliver() can wait for them."""
        handlers = self.ws._handlers
        for key, handler in list(handlers.items()):
            if getattr(handler, "_rig_tracked", False):
                continue

            def tracked(
                message_type: int, message: Any, original: Callable[..., Any] = handler
            ) -> Any:
                result = original(message_type, message)
                if isinstance(result, asyncio.Future):
                    self.tasks.append(result)
                return result

            tracked._rig_tracked = True  # type: ignore[attr-defined]
            handlers[key] = tracked

    async def deliver(self, *messages: Any) -> None:
        """The Qobuz cloud sends one batch to this renderer; wait until it is handled."""
        assert not self.has_left_qobuz_session(), "the speaker left the Qobuz session"
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
        """The app plays the album from track ``index``; waits until Music Assistant plays it."""
        await self.deliver(album_state(index))
        await self.wait_for(
            lambda: (
                self.queue_track() == ALBUM[index]
                and self.queue_state() == MusicAssistantState.PLAYING
            ),
            f"Music Assistant playing {ALBUM[index]}",
        )

    async def play_to_near_end(self, seconds_left: float = 1.0) -> None:
        """Music Assistant plays on to ``seconds_left`` before the end, and the player saw it.

        Music Assistant sends no position ticks: the player's 0.5 s monitor reads the position,
        so the backend only knows the track got near its end once a poll ran.
        """
        self.server.advance_time(
            self.player_id, TRACK_DURATION - seconds_left - self.queue_position()
        )
        await self.wait_for(
            lambda: self.player._position_value_ms >= (TRACK_DURATION - seconds_left - 1) * 1000,
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
            f"next track {track_id} queued in Music Assistant",
        )

    def app(self) -> tuple[int, int]:
        """(playing state, current queue item id) of the last report the app got."""
        assert self.cloud.reports, "the app got no state report"
        report = self.cloud.reports[-1]
        return report.playingState, report.currentQueueItemId

    def app_position(self) -> int:
        return int(self.cloud.reports[-1].currentPosition.value)

    def shown_states(self, since: int = 0) -> set[int]:
        """Every playing state the app was shown, from report number ``since`` on."""
        return {report.playingState for report in self.cloud.reports[since:]}

    def shown_queue_items(self) -> list[int]:
        """The Qobuz queue items the app showed as current, in order, repeats collapsed."""
        shown = [report.currentQueueItemId for report in self.cloud.reports]
        return [item for item, _ in groupby(item for item in shown if item)]

    def has_left_qobuz_session(self) -> bool:
        """The speaker closed its Qobuz connection, so the app no longer shows it playing."""
        return self.cloud.sockets[-1].closed.is_set()

    def queue_track(self) -> Optional[str]:
        queue = self.server.queue(self.player_id)
        if queue.current_index is None:
            return None
        return self.server.track_ids(self.player_id)[queue.current_index]

    def queue_state(self) -> MusicAssistantState:
        return self.server.queue(self.player_id).state

    def queue_position(self) -> float:
        """Seconds into the current track in Music Assistant."""
        return float(self.server.queue(self.player_id).corrected_elapsed_time)

    def plays(self) -> list[tuple[str, list[str]]]:
        """(option, media uris) of every play_media the add-on sent."""
        result = []
        for args in self.server.calls("player_queues/play_media"):
            media = args.get("media")
            result.append((str(args.get("option")), media if isinstance(media, list) else [media]))
        return result

    def volumes_set(self) -> list[int]:
        """Every volume level the add-on set in Music Assistant."""
        return [args["volume_level"] for args in self.server.calls("players/cmd/group_volume")]

    def writes(self) -> list[str]:
        """Each command that changes something in Music Assistant, oldest first."""
        return [name for name, _ in self.server.writes]

    async def wait_for(self, condition: Callable[[], Any], what: str, timeout: float = 3.0) -> None:
        await wait_for(condition, what, server=self.server, timeout=timeout)

    async def idle(self, seconds: float = 0.3) -> None:
        """Let real time pass (timers, the player's 0.5 s monitor) with events flowing."""
        await idle(self.server, seconds)
        await self.drain()


def assert_no_backend_errors(caplog: pytest.LogCaptureFixture) -> None:
    errors = [
        r
        for r in caplog.records
        if r.name.startswith(BACKEND_LOGGER) and r.levelno >= logging.ERROR
    ]
    assert not errors, [r.getMessage() for r in errors]
