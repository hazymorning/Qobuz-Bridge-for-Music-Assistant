"""Full-stack Music Assistant scenarios, one test (or small group) per user requirement.

Real: the Speaker wiring (``Speaker.start`` and ``Speaker._setup_websocket``), the WsManager
connection loop (authenticate, subscribe, join, receive), PlaybackCommandHandler, QueueHandler,
VolumeCommandHandler, StateReporter, QobuzPlayer, QobuzQueue, the shared
MusicAssistantConnection, MusicAssistantBackend and the real music-assistant-client talking to
the in-memory FakeMAServer.

Fake: the Qobuz Connect cloud socket (``QobuzCloud`` decodes every frame the renderer sends,
which is exactly what the Qobuz app gets to see), the Qobuz track API (``StubMetadata``: URL and
metadata per track id; a region-blocked track has neither) and mDNS/HTTP discovery.

Time: Music Assistant playback only moves with ``server.advance_time``; the backend's timers are
patched to milliseconds and every wait is a bounded poll (``Rig.wait_for``).
"""

from __future__ import annotations

import asyncio
import logging
import uuid
from typing import Any, Callable, Optional
from unittest.mock import MagicMock

import pytest

pytest.importorskip("music_assistant_models")
pytest.importorskip("music_assistant_client")

import websockets  # noqa: E402
from music_assistant_models.enums import PlaybackState as MAState  # noqa: E402
from music_assistant_models.enums import PlayerType  # noqa: E402

from qobuz_proxy import speaker as speaker_module  # noqa: E402
from qobuz_proxy.backends.music_assistant import backend as backend_module  # noqa: E402
from qobuz_proxy.backends.music_assistant import connection as connection_module  # noqa: E402
from qobuz_proxy.backends.music_assistant import settings  # noqa: E402
from qobuz_proxy.backends.music_assistant.backend import MusicAssistantBackend  # noqa: E402
from qobuz_proxy.backends.music_assistant.connection import (  # noqa: E402
    ConnectionState,
    MusicAssistantConnection,
)
from qobuz_proxy.backends.music_assistant.manager import device_uuid  # noqa: E402
from qobuz_proxy.backends.types import PlaybackState  # noqa: E402
from qobuz_proxy.config import SpeakerConfig  # noqa: E402
from qobuz_proxy.connect.protocol import DecodedMessage, MessageType, ProtocolCodec  # noqa: E402
from qobuz_proxy.connect.types import ConnectTokens, JWTConnectToken  # noqa: E402
from qobuz_proxy.playback import state_reporter as state_reporter_module  # noqa: E402
from qobuz_proxy.playback.metadata import TrackMetadata  # noqa: E402
from qobuz_proxy.proto import qconnect_common_pb2 as common_pb  # noqa: E402
from qobuz_proxy.proto import qconnect_payload_pb2 as pb  # noqa: E402
from qobuz_proxy.speaker import Speaker  # noqa: E402
from tests.backends.fake_music_assistant import FakeMAServer  # noqa: E402

PID = "up1"
ALBUM = ["101", "102", "103"]
TRACK_S = 200  # FakeMAServer's default track length

# Wire playing states as the Qobuz app sees them
APP_STOPPED, APP_PLAYING, APP_PAUSED = 1, 2, 3
ACTION_NEXT = 2  # RndrSrvrRendererAction.action

BACKEND_LOGGER = "qobuz_proxy.backends.music_assistant"


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


# =============================================================================
# Fakes for the Qobuz side
# =============================================================================


class StubMetadata:
    """Qobuz track API: a URL and metadata per track id; blocked tracks get neither."""

    def __init__(self) -> None:
        self.unavailable: set[str] = set()

    async def get_streaming_url(self, track_id: str) -> Optional[str]:
        if track_id in self.unavailable:
            return None  # "Not available" in this region
        return f"https://streaming.qobuz.test/{track_id}.flac"

    async def get_metadata(self, track_id: str, fetch_url: bool = False) -> Optional[TrackMetadata]:
        if track_id in self.unavailable:
            return None
        return TrackMetadata(
            track_id=track_id,
            title=f"Song {track_id}",
            artist="Artist",
            album="Album",
            duration_ms=TRACK_S * 1000,
            actual_quality=27,
        )

    def get_track_format(self, track_id: str) -> tuple[int, int, int]:
        return (27, 192000, 24)

    def get_track_actual_quality(self, track_id: str) -> Optional[int]:
        return 27

    def get_track_blob(self, track_id: str) -> Optional[str]:
        return None

    def log_now_playing_info(self, metadata: Any, actual_quality: Optional[int] = None) -> None:
        pass


class StubDiscovery:
    """mDNS + discovery HTTP endpoints (not needed: the app 'connects' via _setup_websocket)."""

    def __init__(self, config: Any, app_id: str, **kwargs: Any) -> None:
        self.cleared = 0

    async def start(self) -> None:
        pass

    async def stop(self) -> None:
        pass

    def set_session(self, tokens: ConnectTokens) -> None:
        pass

    def clear_session(self) -> None:
        self.cleared += 1


class CloudSocket:
    """One renderer websocket as the Qobuz cloud holds it."""

    def __init__(self, cloud: QobuzCloud) -> None:
        self.cloud = cloud
        self.closed = asyncio.Event()

    async def __aenter__(self) -> CloudSocket:
        return self

    async def __aexit__(self, *exc: Any) -> bool:
        self.closed.set()
        return False

    async def send(self, frame: bytes) -> None:
        self.cloud.receive(frame)

    async def recv(self) -> bytes:
        await self.closed.wait()
        raise websockets.ConnectionClosed(None, None)

    async def close(self) -> None:
        self.closed.set()


class QobuzCloud:
    """The Qobuz Connect server for one renderer: records what the app is shown.

    Like the real server it answers a report that moved to a new queue item with a SET_STATE
    carrying only ``nextQueueItem``, and a renderer NEXT action with a SET_STATE that plays
    the following album track.
    """

    def __init__(self) -> None:
        self.codec = ProtocolCodec(b"\x00" * 16)
        self.sockets: list[CloudSocket] = []
        self.reports: list[Any] = []
        self.actions: list[int] = []
        self.volumes: list[int] = []
        self.album: list[str] = list(ALBUM)
        self.current_qid = 0
        self._answered_qid = 0
        self.deliver: Optional[Callable[..., Any]] = None  # set by the rig

    def connect(self, *args: Any, **kwargs: Any) -> CloudSocket:
        sock = CloudSocket(self)
        self.sockets.append(sock)
        return sock

    def receive(self, frame: bytes) -> None:
        decoded = self.codec.decode_frame(frame)
        if decoded is None or decoded.msg_type != MessageType.PAYLOAD or not decoded.payload:
            return  # AUTHENTICATE / SUBSCRIBE
        batch = self.codec.decode_qconnect_batch(decoded.payload)
        assert batch is not None
        for msg in batch.messages:
            if msg.messageType == 23:
                state = common_pb.QueueRendererState()
                state.CopyFrom(msg.rndrSrvrStateUpdated.state)
                self.reports.append(state)
                self._on_report(state)
            elif msg.messageType == 24:
                self.actions.append(msg.rndrSrvrRendererAction.action)
                if msg.rndrSrvrRendererAction.action == ACTION_NEXT:
                    self._on_next()
            elif msg.messageType == 25:
                self.volumes.append(msg.rndrSrvrVolumeChanged.volume)

    def _on_report(self, state: Any) -> None:
        qid = state.currentQueueItemId
        if not qid:
            return
        self.current_qid = qid
        if qid == self._answered_qid or self.deliver is None:
            return
        self._answered_qid = qid
        if qid < len(self.album):
            self.deliver(set_state(next_track=(self.album[qid], qid + 1)))

    def _on_next(self) -> None:
        index = self.current_qid  # queue item id n is album index n - 1
        if self.deliver is None or index >= len(self.album):
            return
        nxt = (self.album[index + 1], index + 2) if index + 1 < len(self.album) else None
        self.deliver(
            set_state(APP_PLAYING, track=(self.album[index], index + 1), next_track=nxt, pos=0)
        )


def set_active(active: bool) -> Any:
    msg = pb.QConnectMessage(messageType=43)
    msg.srvrRndrSetActive.active = active
    return msg


def set_state(
    playing_state: Optional[int] = None,
    *,
    track: Optional[tuple[str, int]] = None,
    next_track: Optional[tuple[str, int]] = None,
    pos: Optional[int] = None,
) -> Any:
    """SrvrRndrSetState like the app sends it (track = (track_id, queue_item_id))."""
    msg = pb.QConnectMessage(messageType=41)
    state = msg.srvrRndrSetState
    state.queueVersion.major = 1
    state.queueVersion.minor = 0
    if playing_state is not None:
        state.playingState = playing_state
    if pos is not None:
        state.currentPosition = pos
    if track is not None:
        state.currentQueueItem.trackId = int(track[0])
        state.currentQueueItem.queueItemId = track[1]
    if next_track is not None:
        state.nextQueueItem.trackId = int(next_track[0])
        state.nextQueueItem.queueItemId = next_track[1]
    return msg


def set_volume(level: int) -> Any:
    msg = pb.QConnectMessage(messageType=42)
    msg.srvrRndrSetVolume.volume = level
    return msg


def album_state(index: int, playing_state: int = APP_PLAYING, pos: int = 0) -> Any:
    """The app's SET_STATE for album track ``index`` (queue item id index + 1)."""
    nxt = (ALBUM[index + 1], index + 2) if index + 1 < len(ALBUM) else None
    return set_state(playing_state, track=(ALBUM[index], index + 1), next_track=nxt, pos=pos)


def make_tokens() -> ConnectTokens:
    return ConnectTokens(
        session_id=str(uuid.uuid4()),
        ws_token=JWTConnectToken(jwt="test-jwt", exp=9_999_999_999, endpoint="wss://qobuz.test/ws"),
    )


# =============================================================================
# Rig
# =============================================================================


class Rig:
    """One exposed MA player as a Qobuz Connect device, wired like production."""

    def __init__(
        self,
        server: FakeMAServer,
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

    # -- lifecycle ------------------------------------------------------------

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
            uuid=device_uuid(self.player_id),
            backend_type="music_assistant",
            max_quality=27,
            http_port=8700,
        )
        self.speaker = Speaker(config, MagicMock(), "test-app", backend=backend)
        assert await self.speaker.start()
        return self

    def _save_session(self, session: Optional[dict[str, Any]]) -> None:
        """Keep the backend's session like the manager does (per MA player id)."""
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

    # -- Qobuz app -> renderer -------------------------------------------------

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
            lambda: self.ma_track() == ALBUM[index] and self.ma_state() == MAState.PLAYING,
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

    # -- what the Qobuz app shows ----------------------------------------------

    def app(self) -> tuple[int, int]:
        """(playing state, current queue item id) of the last report the app got."""
        assert self.cloud.reports, "the app got no state report"
        report = self.cloud.reports[-1]
        return report.playingState, report.currentQueueItemId

    def app_position(self) -> int:
        return int(self.cloud.reports[-1].currentPosition.value)

    # -- what Music Assistant does ---------------------------------------------

    def ma_track(self) -> Optional[str]:
        queue = self.server.queue(self.player_id)
        if queue.current_index is None:
            return None
        return self.server.track_ids(self.player_id)[queue.current_index]

    def ma_state(self) -> MAState:
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


def uri(track_id: str) -> str:
    return f"qobuz://track/{track_id}"


def ma_server() -> FakeMAServer:
    server = FakeMAServer()
    server.add_qobuz_provider()
    server.add_player(PID, "Kitchen")
    return server


@pytest.fixture
def fast_heartbeat(monkeypatch: pytest.MonkeyPatch) -> None:
    """State reports every 0.2 s (request it before ``rig``: the reporter reads it per sleep)."""
    monkeypatch.setattr(state_reporter_module, "STATE_UPDATE_INTERVAL_SECONDS", 0.2)


@pytest.fixture
def server() -> FakeMAServer:
    return ma_server()


@pytest.fixture
async def rig(server: FakeMAServer, monkeypatch: pytest.MonkeyPatch):
    rig = await Rig(server, monkeypatch).start()
    await rig.select()
    try:
        yield rig
    finally:
        await rig.close()


def assert_no_backend_errors(caplog: pytest.LogCaptureFixture) -> None:
    errors = [
        r
        for r in caplog.records
        if r.name.startswith(BACKEND_LOGGER) and r.levelno >= logging.ERROR
    ]
    assert not errors, [r.getMessage() for r in errors]


# =============================================================================
# 1. Album start
# =============================================================================


class TestAlbumStart:
    async def test_play_replaces_ma_queue_reports_playing_and_queues_next_gaplessly(self, rig):
        await rig.play_album(0)
        await rig.wait_armed("102")

        assert rig.plays()[:2] == [("replace", [uri("101")]), ("replace_next", [uri("102")])]
        assert len(rig.plays()) == 2
        assert rig.server.track_ids(PID) == ["101", "102"]
        assert rig.ma_track() == "101"
        assert rig.app() == (APP_PLAYING, 1)
        assert rig.player.state == PlaybackState.PLAYING
        # Gapless: MA itself hands the next item to the speaker, the proxy sends no stop/play
        assert "player_queues/stop" not in rig.writes()
        assert "player_queues/play_index" not in rig.writes()


# =============================================================================
# 2. Album plays to the end without gaps or repeats
# =============================================================================


class TestAlbumToTheEnd:
    async def test_three_tracks_play_once_each_in_order_and_stop_at_the_end(self, rig):
        await rig.play_album(0)
        await rig.wait_armed("102")

        await rig.finish_track()
        await rig.wait_for(lambda: rig.app() == (APP_PLAYING, 2), "app shows track 2")
        assert rig.ma_track() == "102"
        await rig.wait_armed("103")

        await rig.finish_track()
        await rig.wait_for(lambda: rig.app() == (APP_PLAYING, 3), "app shows track 3")
        assert rig.ma_track() == "103"

        await rig.finish_track()  # last track: the speaker goes idle
        assert rig.server.mark_ended(PID)
        await rig.wait_for(lambda: rig.app()[0] == APP_STOPPED, "app shows stopped")
        await rig.idle(0.6)

        assert rig.player.state == PlaybackState.STOPPED
        assert rig.player.current_track is None  # a natural end of the queue, not a stop
        assert rig.server.track_ids(PID) == ["101", "102", "103"]
        assert rig.plays() == [
            ("replace", [uri("101")]),
            ("replace_next", [uri("102")]),
            ("replace_next", [uri("103")]),
        ]
        # The app followed every track once, in order (no repeat of the album)
        shown = [r.currentQueueItemId for r in rig.cloud.reports if r.currentQueueItemId]
        assert [qid for i, qid in enumerate(shown) if i == 0 or shown[i - 1] != qid] == [1, 2, 3]
        assert not {"player_queues/stop", "player_queues/play_index", "player_queues/play"} & set(
            rig.writes()
        )
        assert rig.ma_state() == MAState.IDLE


# =============================================================================
# 3. Commands from the Qobuz app reach Music Assistant
# =============================================================================


class TestAppCommands:
    async def test_pause_resume_seek_and_volume_become_ma_commands(self, rig):
        await rig.play_album(0)
        await rig.wait_armed("102")
        rig.server.advance_time(PID, 30)
        rig.server.clear_logs()

        # Pause (the app sends the position it shows: MA is already there, so no seek)
        await rig.deliver(album_state(0, APP_PAUSED, pos=30_000))
        await rig.wait_for(lambda: rig.ma_state() == MAState.PAUSED, "MA paused")
        assert rig.writes() == ["player_queues/pause"]
        assert rig.app() == (APP_PAUSED, 1)

        rig.server.clear_logs()
        await rig.deliver(album_state(0, APP_PLAYING, pos=30_000))
        await rig.wait_for(lambda: rig.ma_state() == MAState.PLAYING, "MA playing")
        assert rig.writes() == ["player_queues/play"]
        assert rig.app() == (APP_PLAYING, 1)

        rig.server.clear_logs()
        await rig.deliver(album_state(0, APP_PLAYING, pos=90_000))
        await rig.wait_for(lambda: rig.server.calls("player_queues/seek"), "MA seek")
        await rig.idle(0.1)
        assert rig.server.calls("player_queues/seek") == [{"queue_id": PID, "position": 90}]
        assert rig.server.queue(PID).corrected_elapsed_time == pytest.approx(90, abs=1)
        assert rig.ma_state() == MAState.PLAYING
        assert rig.app()[0] == APP_PLAYING
        assert rig.app_position() == pytest.approx(90_000, abs=1_500)

        rig.server.clear_logs()
        await rig.deliver(set_volume(35))
        assert rig.server.calls("players/cmd/group_volume") == [
            {"player_id": PID, "volume_level": 35}
        ]
        assert rig.server.players[PID].volume_level == 35
        assert rig.cloud.volumes[-1] == 35

    async def test_next_jumps_to_the_queued_item_and_previous_replaces_the_queue(self, rig):
        await rig.play_album(0)
        await rig.wait_armed("102")
        queued = rig.server.item_ids(PID)[1]
        rig.server.clear_logs()

        await rig.deliver(album_state(1))  # "next" in the app
        await rig.wait_for(
            lambda: rig.ma_track() == "102" and rig.app() == (APP_PLAYING, 2), "track 2"
        )
        jumps = rig.server.calls("player_queues/play_index")
        assert [(c["queue_id"], c["index"]) for c in jumps] == [(PID, queued)]
        assert ("replace", [uri("102")]) not in rig.plays()
        await rig.wait_armed("103")

        rig.server.clear_logs()
        await rig.deliver(album_state(0))  # "previous" in the app
        await rig.wait_for(
            lambda: (
                rig.ma_track() == "101"
                and rig.ma_state() == MAState.PLAYING
                and rig.app() == (APP_PLAYING, 1)
            ),
            "track 1 again",
        )
        assert rig.plays()[0] == ("replace", [uri("101")])
        assert rig.server.track_ids(PID)[0] == "101"


# =============================================================================
# 4. Pause / resume / skip in Music Assistant's UI show in the Qobuz app
# =============================================================================


class TestMAUserInterface:
    async def test_pause_resume_and_next_in_ma_show_in_the_app(self, rig):
        await rig.play_album(0)
        await rig.wait_armed("102")
        rig.server.advance_time(PID, 20)
        rig.server.clear_logs()

        rig.server.ui_pause(PID)
        await rig.wait_for(lambda: rig.app() == (APP_PAUSED, 1), "app shows paused")
        assert rig.player.state == PlaybackState.PAUSED

        rig.server.ui_resume(PID)
        await rig.wait_for(lambda: rig.app() == (APP_PLAYING, 1), "app shows playing")

        rig.server.ui_next(PID)
        await rig.wait_for(lambda: rig.app() == (APP_PLAYING, 2), "app shows track 2")
        assert rig.ma_track() == "102"
        await rig.wait_armed("103")

        # The proxy followed MA and only queued the track after the new one
        assert set(rig.writes()) == {"player_queues/play_media"}
        assert rig.plays() == [("replace_next", [uri("103")])]

    async def test_seek_in_ma_moves_the_apps_progress_bar_within_a_heartbeat(
        self, fast_heartbeat, rig
    ):
        await rig.play_album(0)
        await rig.wait_armed("102")
        rig.server.advance_time(PID, 10)
        await rig.idle(0.2)
        rig.server.clear_logs()

        rig.server.ui_seek(PID, 120)
        await rig.wait_for(
            lambda: rig.app()[0] == APP_PLAYING and abs(rig.app_position() - 120_000) <= 1_500,
            "app shows 2:00",
        )
        assert rig.server.writes == []  # followed, not seeked again

    @pytest.mark.parametrize("left_s", [100, 3], ids=["mid-track", "last-seconds"])
    async def test_stop_in_ma_shows_stopped_and_never_plays_the_next_track(self, rig, left_s):
        await rig.play_album(0)
        await rig.wait_armed("102")
        await rig.play_to_near_end(left_s)
        rig.server.clear_logs()

        rig.server.ui_stop(PID)
        await rig.wait_for(lambda: rig.app()[0] == APP_STOPPED, "app shows stopped")
        await rig.idle(0.6)

        assert rig.server.writes == []  # no auto-advance into the next track
        assert rig.ma_state() == MAState.IDLE and rig.ma_track() == "101"
        assert rig.player.current_track.track_id == "101"  # stopped, not finished
        assert rig.ws.is_renderer_active

    async def test_after_a_stop_in_ma_the_proxy_never_takes_ma_back(self, rig):
        await rig.play_album(0)
        await rig.wait_armed("102")
        rig.server.advance_time(PID, 30)
        rig.server.ui_stop(PID)
        await rig.wait_for(lambda: rig.app()[0] == APP_STOPPED, "app shows stopped")
        rig.server.clear_logs()

        # The user carries on in MA: resume, then MA plays through the queue on its own
        rig.server.ui_resume(PID)
        await rig.idle(0.2)
        rig.server.advance_time(PID, TRACK_S - 31)
        rig.server.finish_current_track(PID)
        await rig.idle(0.2)
        assert rig.ma_track() == "102"
        rig.server.advance_time(PID, TRACK_S - 1)
        rig.server.finish_current_track(PID)
        rig.server.mark_ended(PID)
        await rig.idle(0.6)

        assert rig.server.writes == []  # no re-queue, no replay of track 2, no stop
        assert rig.player.state == PlaybackState.STOPPED

    async def test_previous_in_ma_shows_stopped_but_keeps_the_session(self, rig):
        await rig.play_album(0)
        await rig.wait_armed("102")
        await rig.finish_track()
        await rig.wait_for(lambda: rig.app() == (APP_PLAYING, 2), "app shows track 2")
        await rig.idle(0.2)  # past the start grace of track 2
        rig.server.clear_logs()

        rig.server.ui_previous(PID)  # within 5 s of the start: MA goes back to track 1
        await rig.wait_for(lambda: rig.app()[0] == APP_STOPPED, "app shows stopped")
        await rig.idle(0.6)

        assert rig.ma_track() == "101" and rig.ma_state() == MAState.PLAYING
        assert rig.server.writes == []  # no fight-back
        assert rig.ws.is_renderer_active  # still selected: play in the app works again
        assert not rig.ws._external_playback


# =============================================================================
# 5. Someone plays something else in Music Assistant
# =============================================================================


async def take_over(rig: Rig) -> None:
    await rig.play_album(0)
    await rig.wait_armed("102")
    await rig.idle(0.2)  # past the start grace
    rig.server.clear_logs()
    rig.server.user_plays_foreign(PID, "Jazz Radio")
    await rig.wait_for(lambda: rig.ws._external_playback, "Qobuz session released")


class TestTakeoverInMA:
    async def test_other_music_releases_the_qobuz_session(self, rig):
        await take_over(rig)

        assert rig.player.state == PlaybackState.STOPPED
        assert not rig.ws.is_renderer_active
        assert rig.cloud.sockets[-1].closed.is_set()  # left the session: the app shows stopped
        assert rig.discovery is not None and rig.discovery.cleared == 1
        assert rig.server.writes == []
        queue = rig.server.queue(PID)
        assert queue.current_item.name == "Jazz Radio" and queue.state == MAState.PLAYING

    async def test_no_ma_writes_after_a_takeover_even_for_replayed_snapshots(self, rig):
        await take_over(rig)

        # The cloud replays the old session state (dropped by the released socket) ...
        await rig.deliver(set_active(True), album_state(0, APP_PLAYING, pos=4_000))
        # ... and even if a snapshot reached the player, the backend refuses it
        await rig.player.apply_remote_state(
            track_id="101", queue_item_id=1, position_ms=4_000, playing_state=APP_PLAYING
        )
        await rig.player.apply_remote_state(
            track_id="102", queue_item_id=2, position_ms=0, playing_state=APP_PAUSED
        )
        await rig.player.set_volume(10)
        await rig.idle(0.6)  # the player's monitor keeps polling the backend

        assert rig.server.writes == []
        assert rig.server.queue(PID).current_item.name == "Jazz Radio"
        assert rig.server.players[PID].volume_level != 10

    async def test_selecting_the_device_again_takes_the_player_back(self, rig):
        await take_over(rig)
        assert rig.server.queue(PID).state == MAState.PLAYING  # the radio still plays

        await rig.select()
        await rig.play_album(0)

        assert rig.plays()[0] == ("replace", [uri("101")])
        await rig.wait_for(lambda: rig.app() == (APP_PLAYING, 1), "app shows playing")
        assert rig.ws.is_renderer_active


# =============================================================================
# 6. End of the Qobuz queue
# =============================================================================


class TestEndOfQueue:
    async def test_last_track_ends_stopped_and_ma_is_left_alone(self, rig):
        await rig.play_album(2)  # the album's last track: no next
        await rig.finish_track()
        await rig.wait_for(lambda: rig.app()[0] == APP_STOPPED, "app shows stopped")
        rig.server.clear_logs()

        assert rig.server.mark_ended(PID)
        await rig.deliver(set_state(APP_STOPPED, track=("103", 3), pos=0))  # the app's stop
        await rig.idle(0.6)

        assert rig.server.writes == []
        assert rig.player.state == PlaybackState.STOPPED
        assert rig.ma_state() == MAState.IDLE and rig.server.queue(PID).ended
        assert rig.plays() == []

    async def test_repeat_and_autoplay_in_ma_are_turned_off_so_the_album_ends(self, server, rig):
        from music_assistant_models.enums import RepeatMode

        server.queue(PID).repeat_mode = RepeatMode.ALL
        server.queue(PID).autoplay_enabled = True
        await rig.play_album(2)

        assert server.queue(PID).repeat_mode == RepeatMode.OFF
        assert not server.queue(PID).autoplay_enabled
        await rig.finish_track()
        await rig.wait_for(lambda: rig.app()[0] == APP_STOPPED, "app shows stopped")
        assert rig.server.track_ids(PID) == ["103"]
        assert rig.ma_state() == MAState.IDLE


# =============================================================================
# 7. Tracks that can't be played are skipped
# =============================================================================


class TestUnavailableTracks:
    async def test_region_blocked_track_is_skipped_and_never_sent_to_ma(self, rig):
        rig.metadata.unavailable = {"102"}
        await rig.play_album(0)
        await rig.idle(0.6)  # the player tries to queue track 2: no URL, so nothing reaches MA
        assert all(uri("102") not in media for _, media in rig.plays())

        await rig.finish_track()
        await rig.wait_for(
            lambda: (
                rig.ma_track() == "103"
                and rig.ma_state() == MAState.PLAYING
                and rig.app() == (APP_PLAYING, 3)
            ),
            "track 3 playing",
        )

        assert ACTION_NEXT in rig.cloud.actions  # the renderer asked the app's queue to skip
        assert all(uri("102") not in media for _, media in rig.plays())
        assert "102" not in rig.server.track_ids(PID)

    async def test_track_ma_cannot_play_is_skipped_and_the_album_continues(self, rig):
        rig.server.unplayable_track_ids = {"102"}
        await rig.play_album(0)
        await rig.idle(0.6)

        await rig.finish_track()
        await rig.wait_for(
            lambda: (
                rig.ma_track() == "103"
                and rig.ma_state() == MAState.PLAYING
                and rig.app() == (APP_PLAYING, 3)
            ),
            "track 3 playing",
        )

        assert ACTION_NEXT in rig.cloud.actions
        assert "102" not in rig.server.track_ids(PID)
        assert rig.player.state == PlaybackState.PLAYING


# =============================================================================
# 8. A Music Assistant group as the Qobuz Connect device
# =============================================================================


@pytest.fixture
def group_server() -> FakeMAServer:
    server = FakeMAServer()
    server.add_qobuz_provider()
    server.add_player("m1", "Kitchen", volume=20)
    server.add_player("m2", "Living room", volume=40)
    server.add_player(
        "grp",
        "Downstairs",
        type=PlayerType.GROUP,
        provider="player_group",
        group_members=["m1", "m2"],
    )
    return server


class TestGroupPlayer:
    async def test_group_plays_and_volume_goes_through_group_volume(
        self, group_server, monkeypatch
    ):
        rig = await Rig(group_server, monkeypatch, player_id="grp", name="Downstairs (MA)").start()
        try:
            await rig.select()
            await rig.play_album(0)
            assert rig.plays()[0] == ("replace", [uri("101")])
            assert group_server.calls("player_queues/play_media")[0]["queue_id"] == "grp"
            await rig.wait_for(lambda: rig.app() == (APP_PLAYING, 1), "app shows playing")

            group_server.clear_logs()
            await rig.deliver(set_volume(40))
            assert group_server.calls("players/cmd/group_volume") == [
                {"player_id": "grp", "volume_level": 40}
            ]
            assert group_server.players["m1"].volume_level == 40
            assert group_server.players["m2"].volume_level == 40

            await rig.idle(0.1)  # past the window where MA echoes our own change
            group_server.set_player_volume("grp", 55)  # changed in MA's UI
            await rig.wait_for(
                lambda: rig.cloud.volumes and rig.cloud.volumes[-1] == 55, "app shows 55%"
            )
            assert rig.player._volume == 55
            assert "players/cmd/group_volume" not in [n for n, _ in group_server.writes][1:]
        finally:
            await rig.close()


# =============================================================================
# 9. Music Assistant restarts mid-play
# =============================================================================


class TestMARestart:
    async def test_quick_restart_shows_stopped_and_play_works_after_reconnect(self, rig, caplog):
        await rig.play_album(0)
        await rig.wait_armed("102")
        rig.server.advance_time(PID, 30)
        await rig.idle(0.2)

        rig.server.restart()
        await rig.wait_for(lambda: not rig.conn.is_connected, "connection lost")
        rig.server.come_back()
        await rig.wait_for(lambda: rig.conn.is_connected, "reconnected")
        await rig.wait_for(lambda: rig.app()[0] == APP_STOPPED, "app shows stopped")
        assert rig.player.state == PlaybackState.STOPPED
        assert rig.app_position() == pytest.approx(30_000, abs=2_000)
        assert rig.ws.is_renderer_active  # still selected

        rig.server.clear_logs()
        await rig.deliver(album_state(0, APP_PLAYING, pos=30_000))  # play where it stopped
        await rig.wait_for(
            lambda: (
                rig.ma_track() == "101"
                and rig.ma_state() == MAState.PLAYING
                and rig.app() == (APP_PLAYING, 1)
            ),
            "playing again",
        )
        assert rig.server.queue(PID).corrected_elapsed_time == pytest.approx(30, abs=2)
        assert_no_backend_errors(caplog)

    async def test_long_outage_shows_stopped_after_the_grace_and_recovers(self, rig, caplog):
        await rig.play_album(0)
        await rig.wait_armed("102")
        rig.server.advance_time(PID, 30)

        rig.server.restart()  # and stays down
        await rig.wait_for(lambda: rig.app()[0] == APP_STOPPED, "app shows stopped")
        assert not rig.conn.is_connected
        assert rig.player.state == PlaybackState.STOPPED

        rig.server.come_back()
        await rig.wait_for(lambda: rig.conn.is_connected, "reconnected")
        rig.server.clear_logs()
        await rig.play_album(0)
        await rig.wait_for(lambda: rig.app() == (APP_PLAYING, 1), "app shows playing")
        assert_no_backend_errors(caplog)


# =============================================================================
# 10. The Music Assistant token expires or is revoked
# =============================================================================


class TestTokenRevoked:
    async def test_revoked_token_stops_reconnecting_and_the_app_shows_stopped(self, rig):
        await rig.play_album(0)
        before = len(rig.server.created_clients)

        rig.server.revoke_token(rig.server.token)
        await rig.wait_for(
            lambda: rig.conn.status.state == ConnectionState.AUTH_FAILED, "auth failed"
        )
        assert rig.player.state == PlaybackState.PLAYING  # a short outage is not a stop yet
        await rig.wait_for(lambda: rig.app()[0] == APP_STOPPED, "app shows stopped")
        assert rig.player.state == PlaybackState.STOPPED

        await rig.idle(0.5)  # 25 reconnect delays
        assert len(rig.server.created_clients) - before == 1  # one attempt, then it gave up
        assert rig.conn.status.state == ConnectionState.AUTH_FAILED


# =============================================================================
# 11. The add-on restarts mid-play
# =============================================================================


class TestAddonRestart:
    async def _restart_mid_play(self, server, monkeypatch, tmp_path) -> tuple[Rig, list[str]]:
        """Play, stop the add-on (MA keeps playing), start a new process, re-select."""
        first = await Rig(server, monkeypatch).start()
        await first.select()
        await first.play_album(0)
        await first.wait_armed("102")
        server.advance_time(PID, 60)
        await first.close()  # the add-on stops; MA keeps playing

        assert "player_queues/stop" not in first.writes()
        assert server.queue(PID).state == MAState.PLAYING
        items_before = server.item_ids(PID)
        # A new process reads the remembered session from the settings file
        path = tmp_path / "music_assistant.json"
        settings.save(path, settings.Settings(sessions=first.sessions))
        server.clear_logs()

        second = await Rig(server, monkeypatch, sessions=settings.load(path).sessions).start()
        await second.select()
        # After the reconnect the Qobuz server replays its snapshot from before (paused at an
        # old position), then the app plays
        await second.deliver(album_state(0, APP_PAUSED, pos=4_000))
        await second.deliver(album_state(0, APP_PLAYING, pos=4_000))
        await second.wait_for(lambda: second.player.state == PlaybackState.PLAYING, "playing")
        return second, items_before

    async def test_restart_mid_play_takes_over_what_ma_plays_without_restarting_it(
        self, fast_heartbeat, server, monkeypatch, tmp_path
    ):
        second, _ = await self._restart_mid_play(server, monkeypatch, tmp_path)
        try:
            await second.wait_for(
                lambda: second.app()[0] == APP_PLAYING and second.app_position() >= 59_000,
                "the app follows MA's position",
            )
            # No restart of the track, no seek back to the stale 0:04, no stop or pause
            assert all(uri("101") not in media for _, media in second.plays())
            assert not {
                "player_queues/stop",
                "player_queues/pause",
                "player_queues/play",
                "player_queues/seek",
                "player_queues/play_index",
                "player_queues/clear",
            } & set(second.writes())
            assert server.queue(PID).corrected_elapsed_time >= 60
            assert second.ma_track() == "101"
            assert server.queue(PID).state == MAState.PLAYING

            # The album goes on with the next track
            await second.finish_track()
            await second.wait_for(lambda: second.app() == (APP_PLAYING, 2), "app shows track 2")
            assert second.ma_track() == "102"
        finally:
            await second.close()

    async def test_restart_keeps_the_next_track_ma_already_has_queued(
        self, fast_heartbeat, server, monkeypatch, tmp_path
    ):
        second, items_before = await self._restart_mid_play(server, monkeypatch, tmp_path)
        try:
            await second.wait_for(lambda: second.player._gapless_armed, "next track armed")
            await second.idle(0.6)
            assert second.writes() == []
            assert server.item_ids(PID) == items_before
        finally:
            await second.close()


# =============================================================================
# 12. Switching to another Qobuz Connect device and back
# =============================================================================


class TestSwitchQobuzDevice:
    async def test_switching_away_stops_ma_once_and_switching_back_plays(self, rig):
        await rig.play_album(0)
        await rig.wait_armed("102")
        rig.server.advance_time(PID, 40)
        rig.server.clear_logs()

        await rig.deliver(set_active(False))
        await rig.wait_for(lambda: rig.ma_state() == MAState.IDLE, "MA stopped")
        assert rig.writes() == ["player_queues/stop"]

        # The session goes on with the other device
        await rig.deliver(album_state(0, APP_PLAYING, pos=41_000))
        await rig.deliver(set_active(False))
        await rig.deliver(set_state(APP_STOPPED, track=("101", 1), pos=41_000))
        await rig.idle(0.6)
        assert rig.writes() == ["player_queues/stop"]
        assert rig.player.state == PlaybackState.STOPPED

        await rig.deliver(set_active(True), album_state(0, APP_PLAYING, pos=0))
        await rig.wait_for(
            lambda: (
                rig.ma_track() == "101"
                and rig.ma_state() == MAState.PLAYING
                and rig.app() == (APP_PLAYING, 1)
            ),
            "playing here again",
        )


# =============================================================================
# 13. Music Assistant unreachable when the add-on starts
# =============================================================================


class TestMAUnreachableAtBoot:
    async def test_play_fails_cleanly_while_ma_is_down_and_works_once_it_is_back(
        self, server, monkeypatch, caplog
    ):
        server.reachable = False
        rig = await Rig(server, monkeypatch).start(wait_for_ma=False)
        try:
            assert rig.conn.status.state in (
                ConnectionState.CONNECTING,
                ConnectionState.DISCONNECTED,
            )
            await rig.select()  # the device is visible and selectable anyway
            await rig.deliver(album_state(0))
            await rig.wait_for(lambda: rig.player.state == PlaybackState.ERROR, "player error")
            assert rig.app()[0] == APP_STOPPED
            assert server.command_log == []

            server.reachable = True
            await rig.wait_for(lambda: rig.conn.is_connected, "MA connected")
            await rig.play_album(0)
            await rig.wait_for(lambda: rig.app() == (APP_PLAYING, 1), "app shows playing")
            assert rig.plays()[0] == ("replace", [uri("101")])
            assert_no_backend_errors(caplog)
        finally:
            await rig.close()
