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
from qobuz_proxy.backends.music_assistant import settings as settings_module  # noqa: E402
from qobuz_proxy.backends.music_assistant.connection import (  # noqa: E402
    MAConnectionState,
    MusicAssistantConnection,
)
from qobuz_proxy.backends.music_assistant.manager import ma_device_uuid  # noqa: E402
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
    }.items():
        monkeypatch.setattr(backend_module, name, value)
    if hasattr(backend_module, "EXPECT_STATE_SECONDS"):
        monkeypatch.setattr(backend_module, "EXPECT_STATE_SECONDS", 0.5)
    monkeypatch.setattr(MusicAssistantConnection, "RECONNECT_DELAYS_SECONDS", (0.02,))


# =============================================================================
# Fakes for the Qobuz side
# =============================================================================


class StubMetadata:
    """Qobuz track API: a URL and metadata per track id; blocked tracks get neither."""

    def __init__(self) -> None:
        self.unavailable: set[str] = set()
        self.url_requests: list[str] = []
        self._max_quality = 27

    @property
    def max_quality(self) -> int:
        return self._max_quality

    def set_max_quality(self, quality: int) -> None:
        self._max_quality = quality

    def is_unavailable(self, track_id: str) -> bool:
        return track_id in self.unavailable

    async def get_streaming_url(self, track_id: str) -> Optional[str]:
        self.url_requests.append(track_id)
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
        self.sessions: list[ConnectTokens] = []
        self.cleared = 0

    async def start(self) -> None:
        pass

    async def stop(self) -> None:
        pass

    def set_session(self, tokens: ConnectTokens) -> None:
        self.sessions.append(tokens)

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
        self.joins: list[Any] = []
        self.reports: list[Any] = []
        self.actions: list[int] = []
        self.volumes: list[int] = []
        self.album: list[str] = list(ALBUM)
        self.answer_reports = True
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
            if msg.messageType == 21:
                self.joins.append(msg.rndrSrvrJoinSession)
            elif msg.messageType == 23:
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
        if not self.answer_reports or qid == self._answered_qid or self.deliver is None:
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
        ws_token=JWTConnectToken(
            jwt="test-jwt", exp=9_999_999_999, endpoint="wss://qobuz.test/ws"
        ),
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
    ) -> None:
        self.server = server
        self.player_id = player_id
        self.name = name
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
        config = SpeakerConfig(
            name=self.name,
            uuid=ma_device_uuid(self.player_id),
            backend_type="music_assistant",
            max_quality=27,
            http_port=8700,
            ma_player_id=self.player_id,
        )
        self.speaker = Speaker(config, MagicMock(), "test-app", ma_connection=self.conn)
        assert await self.speaker.start()
        return self

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
    def backend(self) -> backend_module.MusicAssistantBackend:
        assert self.speaker is not None
        backend = self.speaker._backend
        assert isinstance(backend, backend_module.MusicAssistantBackend)
        return backend

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

    async def wait_armed(self, track_id: str) -> None:
        await self.wait_for(
            lambda: self.player._gapless_armed
            and self.player._pending_next_track["trackId"] == track_id
            and track_id in self.server.track_ids(self.player_id),
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

        rig.server.advance_time(PID, TRACK_S - 3)
        rig.server.finish_current_track(PID)
        await rig.wait_for(lambda: rig.app() == (APP_PLAYING, 2), "app shows track 2")
        assert rig.ma_track() == "102"
        await rig.wait_armed("103")

        rig.server.advance_time(PID, TRACK_S - 3)
        rig.server.finish_current_track(PID)
        await rig.wait_for(lambda: rig.app() == (APP_PLAYING, 3), "app shows track 3")
        assert rig.ma_track() == "103"

        rig.server.advance_time(PID, TRACK_S - 3)
        rig.server.finish_current_track(PID)  # last track: the speaker goes idle
        assert rig.server.mark_ended(PID)
        await rig.wait_for(lambda: rig.app()[0] == APP_STOPPED, "app shows stopped")
        await rig.idle(0.6)

        assert rig.player.state == PlaybackState.STOPPED
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
