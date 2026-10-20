"""The Qobuz side of the tests: the Connect cloud, the track API and discovery stubs.

``QobuzCloud`` decodes every frame the renderer sends, which is exactly what the Qobuz app
gets to see. The message builders make the frames the app sends to the renderer.
"""

from __future__ import annotations

import asyncio
import uuid
from typing import Any, Callable, Optional

import websockets

from qobuz_proxy.connect.protocol import MessageType, ProtocolCodec
from qobuz_proxy.connect.types import ConnectTokens, JWTConnectToken
from qobuz_proxy.playback.metadata import TrackMetadata
from qobuz_proxy.proto import qconnect_common_pb2 as common_pb
from qobuz_proxy.proto import qconnect_payload_pb2 as pb

ALBUM = ["101", "102", "103"]
TRACK_S = 200  # FakeMusicAssistantServer's default track length

# Wire playing states as the Qobuz app sees them
APP_STOPPED, APP_PLAYING, APP_PAUSED = 1, 2, 3
ACTION_NEXT = 2  # RndrSrvrRendererAction.action


def uri(track_id: str) -> str:
    return f"qobuz://track/{track_id}"


class StubMetadata:
    """Qobuz track API: a URL and metadata per track id; blocked tracks get neither."""

    def __init__(self) -> None:
        self.unavailable: set[str] = set()
        self.max_qualities: list[int] = []  # every quality the app chose

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

    def set_max_quality(self, quality: int) -> None:
        self.max_qualities.append(quality)

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
        queue_id = state.currentQueueItemId
        if not queue_id:
            return
        self.current_qid = queue_id
        if queue_id == self._answered_qid or self.deliver is None:
            return
        self._answered_qid = queue_id
        if queue_id < len(self.album):
            self.deliver(set_state(next_track=(self.album[queue_id], queue_id + 1)))

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
