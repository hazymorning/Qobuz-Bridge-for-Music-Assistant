"""MusicAssistantBackend commands and gapless arming, against the fake MA server.

Unit level: the backend talks to a real MusicAssistantConnection whose client is the
real ``music-assistant-client`` wired to the in-memory server; the Player is replaced by
mocks on the backend callbacks. The backend's timing constants are patched to tiny
values (see ``FAST_TIMING``) and every wait is a bounded poll.

The ``Rig`` here is shared with ``test_ma_events.py``.
"""

import asyncio
import logging
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from typing import Any, Callable, Optional
from unittest.mock import AsyncMock, MagicMock

import pytest

pytest.importorskip("music_assistant_models")
pytest.importorskip("music_assistant_client")

from music_assistant_models.enums import PlaybackState as MAState  # noqa: E402
from music_assistant_models.enums import PlayerType, RepeatMode  # noqa: E402
from music_assistant_models.errors import (  # noqa: E402
    InvalidCommand,
    InvalidProviderURI,
    MediaNotFoundError,
    PlayerCommandFailed,
    UnplayableMediaError,
)

from qobuz_proxy.backends.base import TrackUnavailableError  # noqa: E402
from qobuz_proxy.backends.music_assistant import backend as backend_module  # noqa: E402
from qobuz_proxy.backends.music_assistant.backend import MusicAssistantBackend  # noqa: E402
from qobuz_proxy.backends.music_assistant.connection import (  # noqa: E402
    MANotConnectedError,
    MusicAssistantConnection,
)
from qobuz_proxy.backends.music_assistant.settings import SessionStore  # noqa: E402
from qobuz_proxy.backends.types import BackendTrackMetadata, PlaybackState  # noqa: E402
from tests.backends.fake_music_assistant import FakeMAServer  # noqa: E402

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
    "STREAM_SETTLE_SECONDS": 0.05,
    "STALL_CONFIRM_SECONDS": 0.05,
    "PROMOTE_SETTLE_SECONDS": 0.1,
}


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

    def wire(self, backend: MusicAssistantBackend) -> "Callbacks":
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


NOTHING_FIRED = {"track_ended": 0, "next_started": 0, "interrupted": 0, "external": 0}


class Rig:
    """Fake MA server + real connection + one backend with mocked Player callbacks."""

    def __init__(self, server: FakeMAServer, conn: MusicAssistantConnection) -> None:
        self.server = server
        self.conn = conn
        self.sessions = SessionStore()
        self.backends: list[MusicAssistantBackend] = []
        self.backend, self.cb = self.make_backend()

    def make_backend(
        self,
        player_id: str = QID,
        name: str = NAME,
        sessions: Optional[SessionStore] = None,
        conn: Optional[MusicAssistantConnection] = None,
    ) -> tuple[MusicAssistantBackend, Callbacks]:
        backend = MusicAssistantBackend(
            conn or self.conn,
            player_id,
            name,
            session_store=sessions if sessions is not None else self.sessions,
        )
        self.backends.append(backend)
        return backend, Callbacks().wire(backend)

    # --- waiting -------------------------------------------------------------------------

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

    # --- driving the backend like the Player does ---------------------------------------

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

    # --- reading MA -----------------------------------------------------------------------

    def queue(self) -> Any:
        return self.server.queue(QID)

    def ma_state(self) -> MAState:
        return self.server.queue(QID).state

    def ma_track(self) -> Optional[str]:
        item = self.server.queue(QID).current_item
        return item.media_item.item_id if item is not None and item.media_item else None

    def ma_item_id(self, index: int) -> str:
        return self.server.item_ids(QID)[index]

    def calls(self, command: str) -> list[dict[str, Any]]:
        return self.server.calls(command)

    def write_names(self) -> list[str]:
        return [name for name, _ in self.server.writes]


def patch_timing(monkeypatch: pytest.MonkeyPatch) -> Callable[..., None]:
    """Patch the backend's timing constants; returns a function that overrides some."""

    def apply(**values: float) -> None:
        for name, value in values.items():
            monkeypatch.setattr(backend_module, name, value)

    # Constants this backend version does not have are skipped (an override of one fails)
    apply(**{k: v for k, v in FAST_TIMING.items() if hasattr(backend_module, k)})
    monkeypatch.setattr(MusicAssistantConnection, "RECONNECT_DELAYS_SECONDS", (0.01,))
    return apply


@asynccontextmanager
async def running_rig() -> AsyncIterator[Rig]:
    """A connected rig; patch the timing first."""
    server = FakeMAServer()
    server.add_qobuz_provider()
    server.add_player(QID, "Kitchen")
    conn = MusicAssistantConnection(server.url, server.token, client_factory=server.client_factory)
    await conn.start()
    r = Rig(server, conn)
    try:
        await r.wait(lambda: conn.is_connected, what="MA connection")
        await r.backend.connect()
        yield r
    finally:
        for backend in r.backends:
            await backend.disconnect()
        await conn.stop()


@pytest.fixture
def timing(monkeypatch: pytest.MonkeyPatch) -> Callable[..., None]:
    return patch_timing(monkeypatch)


@pytest.fixture
async def rig(timing: Callable[..., None]) -> AsyncIterator[Rig]:
    async with running_rig() as r:
        yield r


# ----------------------------------------------------------------------------- basics


class TestBasics:
    async def test_info_and_capabilities(self, rig: Rig) -> None:
        info = rig.backend.get_info()
        assert (info.backend_type, info.name, info.device_id) == ("music_assistant", NAME, QID)
        assert rig.backend.player_id == QID
        assert rig.backend.reports_external_resume is True
        assert rig.backend.supports_gapless is True
        assert rig.backend.is_connected()

    async def test_nothing_played_reads_stopped_without_calling_ma(self, rig: Rig) -> None:
        commands = len(rig.server.command_log)
        assert await rig.backend.get_state() == PlaybackState.STOPPED
        assert await rig.backend.get_position() == 0
        assert len(rig.server.command_log) == commands

    async def test_disconnect_is_idempotent_and_leaves_ma_playing(self, rig: Rig) -> None:
        await rig.play("101")
        await rig.backend.disconnect()
        await rig.backend.disconnect()
        await rig.settle()
        assert rig.ma_state() == MAState.PLAYING
        assert rig.calls("player_queues/stop") == []
        assert not rig.backend.is_connected()

    async def test_connect_succeeds_while_ma_is_down(self, rig: Rig) -> None:
        rig.server.set_reachable(False, drop=True)
        await rig.wait(lambda: not rig.conn.is_connected)
        backend, _cb = rig.make_backend()

        assert await backend.connect() is True
        assert await backend.connect() is True  # idempotent
        assert await backend.get_state() == PlaybackState.STOPPED


# ------------------------------------------------------------------------------- play


class TestPlay:
    async def test_play_replaces_the_queue_with_the_qobuz_track(self, rig: Rig) -> None:
        await rig.play("101")

        (call,) = rig.calls("player_queues/play_media")
        assert call["queue_id"] == QID
        assert call["media"] == "qobuz://track/101"
        assert call["option"] == "replace"
        assert rig.server.track_ids(QID) == ["101"]
        assert rig.ma_state() == MAState.PLAYING
        assert rig.backend._current is not None
        assert rig.backend._current.item_id == rig.ma_item_id(0)
        assert rig.backend._current.track_id == "101"
        assert rig.backend._attached is True
        assert await rig.backend.get_state() == PlaybackState.PLAYING
        rig.cb.state.assert_called_with(PlaybackState.PLAYING)
        assert rig.calls("player_queues/clear") == []
        assert rig.calls("player_queues/play_index") == []

    async def test_play_replaces_what_ma_was_playing(self, rig: Rig) -> None:
        rig.server.start_playing(QID, ["900", "901"])
        await rig.settle()

        await rig.play("101")

        assert rig.server.track_ids(QID) == ["101"]
        assert rig.cb.endings() == NOTHING_FIRED

    async def test_play_logs_the_command(self, rig: Rig, caplog: pytest.LogCaptureFixture) -> None:
        with caplog.at_level(logging.INFO):
            await rig.play("101")
        assert f"[{NAME}] Qobuz: play track 101 'Song 101' – Artist" in caplog.text
        assert "MA → player_queues/play_media queue=up1 media=qobuz://track/101" in caplog.text

    async def test_positioned_start_seeks_in_one_step(self, rig: Rig) -> None:
        await rig.play("101", start_ms=65_000)

        assert rig.write_names()[:3] == [
            "player_queues/clear",
            "player_queues/play_media",
            "player_queues/play_index",
        ]
        assert rig.calls("player_queues/play_media")[0]["option"] == "add"
        (jump,) = rig.calls("player_queues/play_index")
        assert jump["index"] == rig.ma_item_id(0)
        assert jump["seek_position"] == 65
        assert rig.queue().corrected_elapsed_time == pytest.approx(65, abs=1)
        assert await rig.backend.get_position() == pytest.approx(65_000, abs=1_000)

        # The Player seeks right after play(): MA is already there
        await rig.backend.seek(65_000)
        await rig.settle()
        assert rig.calls("player_queues/seek") == []
        assert rig.calls("player_queues/play_index") == [jump]

    async def test_start_below_two_seconds_is_a_normal_start(self, rig: Rig) -> None:
        await rig.play("101", start_ms=1_500)
        assert [c["option"] for c in rig.calls("player_queues/play_media")] == ["replace"]
        assert rig.calls("player_queues/play_index") == []

    async def test_play_without_ma_raises_not_connected(self, rig: Rig) -> None:
        rig.server.set_reachable(False, drop=True)
        await rig.wait(lambda: not rig.conn.is_connected)
        writes = list(rig.server.writes)

        with pytest.raises(MANotConnectedError):
            await rig.backend.play(STREAM_URL, meta("101"))

        assert rig.server.writes == writes
        assert await rig.backend.get_state() == PlaybackState.STOPPED

    @pytest.mark.parametrize(
        "how",
        ["unplayable", "media_not_found", "unplayable_media", "invalid_uri", "stream_failure"],
    )
    async def test_track_ma_cannot_play_raises_track_unavailable(self, rig: Rig, how: str) -> None:
        if how == "unplayable":
            rig.server.unplayable_track_ids.add("101")
        elif how == "media_not_found":
            rig.server.fail_next["player_queues/play_media"] = MediaNotFoundError("not found")
        elif how == "unplayable_media":
            rig.server.fail_next["player_queues/play_media"] = UnplayableMediaError("region")
        elif how == "invalid_uri":
            rig.server.fail_next["player_queues/play_media"] = InvalidProviderURI("bad uri")
        else:
            rig.server.stream_failure_track_ids.add("101")

        with pytest.raises(TrackUnavailableError) as err:
            await rig.backend.play(STREAM_URL, meta("101"))
        await rig.settle()

        assert isinstance(err.value, RuntimeError)
        assert await rig.backend.get_state() == PlaybackState.STOPPED
        assert rig.cb.endings() == NOTHING_FIRED

    async def test_unavailable_player_is_a_plain_error(self, rig: Rig) -> None:
        rig.server.update_player(QID, available=False)
        await rig.settle()

        with pytest.raises(RuntimeError) as err:
            await rig.backend.play(STREAM_URL, meta("101"))

        assert not isinstance(err.value, TrackUnavailableError)
        assert not isinstance(err.value, MANotConnectedError)

    async def test_library_item_is_recognised_as_ours(self, rig: Rig) -> None:
        rig.server.library_track_ids.add("101")

        await rig.play("101")

        current = rig.queue().current_item
        assert current is not None and current.media_item.provider == "library"
        assert rig.backend._current is not None
        assert rig.backend._current.item_id == current.queue_item_id
        rig.server.ui_pause(QID)
        await rig.settle()
        assert await rig.backend.get_state() == PlaybackState.PAUSED
        assert rig.cb.endings() == NOTHING_FIRED

    async def test_play_again_after_an_error_works(self, rig: Rig) -> None:
        rig.server.fail_next["player_queues/play_media"] = MediaNotFoundError("not found")
        with pytest.raises(TrackUnavailableError):
            await rig.backend.play(STREAM_URL, meta("101"))

        await rig.play("102")

        assert rig.ma_track() == "102"
        assert await rig.backend.get_state() == PlaybackState.PLAYING


# ------------------------------------------------------------------------ pause/resume


class TestPauseResume:
    async def test_pause_and_resume(self, rig: Rig) -> None:
        await rig.play("101")

        await rig.backend.pause()
        await rig.settle()
        assert [c["queue_id"] for c in rig.calls("player_queues/pause")] == [QID]
        assert rig.ma_state() == MAState.PAUSED
        assert await rig.backend.get_state() == PlaybackState.PAUSED
        rig.cb.state.assert_called_with(PlaybackState.PAUSED)

        assert await rig.backend.resume() is True
        await rig.settle()
        assert len(rig.calls("player_queues/play")) == 1
        assert rig.ma_state() == MAState.PLAYING
        assert await rig.backend.get_state() == PlaybackState.PLAYING
        assert rig.cb.endings() == NOTHING_FIRED

    async def test_pause_without_ma_raises_not_connected(self, rig: Rig) -> None:
        await rig.play("101")
        rig.server.set_reachable(False, drop=True)
        await rig.wait(lambda: not rig.conn.is_connected)
        with pytest.raises(MANotConnectedError):
            await rig.backend.pause()

    async def test_resume_without_ma_returns_false(self, rig: Rig) -> None:
        await rig.play("101")
        await rig.backend.pause()
        rig.server.set_reachable(False, drop=True)
        await rig.wait(lambda: not rig.conn.is_connected)
        assert await rig.backend.resume() is False

    async def test_resume_after_mas_paused_auto_stop(self, rig: Rig) -> None:
        await rig.play_for("101", 40)
        await rig.backend.pause()
        assert rig.server.auto_stop_after_pause(QID)  # MA stops a queue paused for 30 s
        await rig.settle()
        assert rig.ma_state() == MAState.IDLE
        assert await rig.backend.get_state() == PlaybackState.PAUSED

        assert await rig.backend.resume() is True
        await rig.settle()

        assert len(rig.calls("player_queues/play")) == 1
        assert rig.calls("player_queues/play_media")[-1]["option"] == "replace"  # no re-play
        assert rig.ma_state() == MAState.PLAYING
        assert rig.queue().corrected_elapsed_time == pytest.approx(40, abs=1)
        assert await rig.backend.get_state() == PlaybackState.PLAYING
        assert rig.cb.endings() == NOTHING_FIRED

    async def test_resume_after_ma_restarted_while_paused(self, rig: Rig) -> None:
        await rig.play_for("101", 40)
        await rig.backend.pause()
        rig.server.restart()
        await rig.wait(lambda: not rig.conn.is_connected)
        rig.server.come_back()
        await rig.wait(lambda: rig.conn.is_connected)
        assert await rig.backend.get_state() == PlaybackState.PAUSED

        assert await rig.backend.resume() is True
        await rig.settle()

        assert len(rig.calls("player_queues/play")) == 1
        assert rig.ma_track() == "101" and rig.ma_state() == MAState.PLAYING
        assert rig.queue().corrected_elapsed_time == pytest.approx(40, abs=1)
        assert rig.cb.endings() == NOTHING_FIRED

    async def test_resume_when_ma_lost_the_item_replays_at_the_position(self, rig: Rig) -> None:
        await rig.play_for("101", 40)
        await rig.backend.pause()
        rig.server.set_reachable(False, drop=True)  # MA goes away while paused...
        await rig.wait(lambda: not rig.conn.is_connected)
        rig.server.remove_player(QID)  # ...and comes back without our queue items
        rig.server.add_player(QID, "Kitchen")
        rig.server.set_reachable(True)
        await rig.wait(lambda: rig.conn.is_connected)
        assert rig.server.track_ids(QID) == []

        assert await rig.backend.resume() is True
        await rig.settle()

        assert rig.ma_track() == "101"
        assert rig.ma_state() == MAState.PLAYING
        assert rig.calls("player_queues/play_index")[-1]["seek_position"] == 40
        assert rig.queue().corrected_elapsed_time == pytest.approx(40, abs=1)
        assert await rig.backend.get_state() == PlaybackState.PLAYING

    async def test_resume_with_nothing_played_returns_false(self, rig: Rig) -> None:
        assert await rig.backend.resume() is False
        assert rig.server.writes == []


# -------------------------------------------------------------------------------- stop


class TestStop:
    async def test_stop_stops_ma_when_it_plays_our_track(self, rig: Rig) -> None:
        await rig.play("101")

        await rig.backend.stop()
        await rig.settle()

        assert [c["queue_id"] for c in rig.calls("player_queues/stop")] == [QID]
        assert rig.ma_state() == MAState.IDLE
        assert await rig.backend.get_state() == PlaybackState.STOPPED
        rig.cb.state.assert_called_with(PlaybackState.STOPPED)
        await rig.idle(0.2)
        assert rig.cb.endings() == NOTHING_FIRED

    async def test_stop_of_a_paused_track_stops_ma(self, rig: Rig) -> None:
        await rig.play("101")
        await rig.backend.pause()
        await rig.backend.stop()
        assert len(rig.calls("player_queues/stop")) == 1

    async def test_stop_sends_nothing_when_ma_is_idle(self, rig: Rig) -> None:
        await rig.play_for("101", 30)
        rig.server.ui_stop(QID)
        await rig.wait(lambda: rig.cb.interrupted.called)
        writes = list(rig.server.writes)

        await rig.backend.stop()
        await rig.settle()

        assert rig.server.writes == writes

    async def test_stop_sends_nothing_while_ma_plays_something_else(self, rig: Rig) -> None:
        # e.g. the speaker's own Qobuz Connect (or MA) is playing; we never played here
        rig.server.user_plays_foreign(QID, "Radio 1")
        await rig.settle()
        await rig.backend.stop()
        await rig.settle()
        assert rig.server.writes == []
        assert rig.ma_state() == MAState.PLAYING

    async def test_stop_while_ma_is_down_does_not_raise(self, rig: Rig) -> None:
        await rig.play("101")
        rig.server.set_reachable(False, drop=True)
        await rig.wait(lambda: not rig.conn.is_connected)
        await rig.backend.stop()
        assert await rig.backend.get_state() == PlaybackState.STOPPED

    async def test_stop_for_the_armed_next_track_sends_nothing_and_play_jumps(
        self, rig: Rig
    ) -> None:
        await rig.play("101")
        assert await rig.arm("102", queue_item_id=7)
        armed_item = rig.ma_item_id(1)
        writes = list(rig.server.writes)

        await rig.backend.stop(next_track_id="102")
        await rig.settle()
        assert rig.server.writes == writes
        rig.cb.state.assert_called_with(PlaybackState.STOPPED)

        await rig.play("102")

        (jump,) = rig.calls("player_queues/play_index")
        assert jump["index"] == armed_item
        assert jump["seek_position"] == 0
        assert len(rig.calls("player_queues/play_media")) == 2  # REPLACE + REPLACE_NEXT only
        assert rig.ma_track() == "102"
        assert rig.backend._current is not None and rig.backend._current.item_id == armed_item
        assert await rig.backend.get_state() == PlaybackState.PLAYING
        await rig.idle(0.1)  # past JUMP_WAIT: no deferred stop
        assert rig.calls("player_queues/stop") == []
        assert rig.cb.endings() == NOTHING_FIRED

    async def test_jump_to_the_armed_track_with_a_start_position(self, rig: Rig) -> None:
        await rig.play("101")
        assert await rig.arm("102")
        await rig.backend.stop(next_track_id="102")

        await rig.play("102", start_ms=30_000)

        assert rig.calls("player_queues/play_index")[-1]["seek_position"] == 30
        assert rig.queue().corrected_elapsed_time == pytest.approx(30, abs=1)

    async def test_jump_wait_expiry_stops_ma(self, rig: Rig) -> None:
        await rig.play("101")
        assert await rig.arm("102")

        await rig.backend.stop(next_track_id="102")  # ...but no play() follows
        await rig.wait(lambda: len(rig.calls("player_queues/stop")) == 1, what="deferred stop")

        assert rig.ma_state() == MAState.IDLE
        assert rig.cb.endings() == NOTHING_FIRED

    async def test_stop_for_another_track_stops_ma_at_once(self, rig: Rig) -> None:
        await rig.play("101")
        assert await rig.arm("102")
        await rig.backend.stop(next_track_id="555")
        assert len(rig.calls("player_queues/stop")) == 1

    async def test_stop_forgets_the_armed_track(self, rig: Rig) -> None:
        await rig.play("101")
        assert await rig.arm("102")
        await rig.backend.stop()

        await rig.play("102")

        assert rig.calls("player_queues/play_index") == []
        assert rig.calls("player_queues/play_media")[-1]["option"] == "replace"
        assert rig.server.track_ids(QID) == ["102"]


# -------------------------------------------------------------------------------- seek


class TestSeek:
    async def test_seek_sends_whole_seconds(self, rig: Rig) -> None:
        await rig.play("101")

        await rig.backend.seek(95_400)
        await rig.settle()

        assert rig.calls("player_queues/seek") == [{"queue_id": QID, "position": 95}]
        assert rig.queue().corrected_elapsed_time == pytest.approx(95, abs=1)
        assert await rig.backend.get_position() == pytest.approx(95_000, abs=1_000)
        assert rig.cb.endings() == NOTHING_FIRED

    async def test_duplicate_scrub_is_sent_once(self, rig: Rig) -> None:
        await rig.play("101")
        await rig.backend.seek(95_000)
        await rig.backend.seek(95_000)
        await rig.backend.seek(95_800)
        assert len(rig.calls("player_queues/seek")) == 1

    async def test_seek_close_to_where_ma_is_is_skipped(self, rig: Rig) -> None:
        await rig.play_for("101", 30)
        await rig.backend.seek(30_800)
        assert rig.calls("player_queues/seek") == []

    async def test_seek_far_from_where_ma_is_is_sent(self, rig: Rig) -> None:
        await rig.play_for("101", 30)
        await rig.backend.seek(10_000)
        assert rig.calls("player_queues/seek") == [{"queue_id": QID, "position": 10}]

    async def test_seek_when_ma_is_not_playing_our_track_raises(self, rig: Rig) -> None:
        with pytest.raises(RuntimeError):
            await rig.backend.seek(10_000)

        await rig.play_for("101", 30)
        rig.server.ui_stop(QID)
        await rig.wait(lambda: rig.cb.interrupted.called)
        with pytest.raises(RuntimeError, match="not playing this Qobuz track"):
            await rig.backend.seek(50_000)
        assert rig.calls("player_queues/seek") == []

    async def test_seek_without_ma_raises_not_connected(self, rig: Rig) -> None:
        await rig.play("101")
        rig.server.set_reachable(False, drop=True)
        await rig.wait(lambda: not rig.conn.is_connected)
        with pytest.raises(MANotConnectedError):
            await rig.backend.seek(100_000)


# ------------------------------------------------------------------------------ volume


class TestVolume:
    async def test_set_volume_uses_group_volume(self, rig: Rig) -> None:
        await rig.backend.set_volume(40)
        await rig.settle()

        assert rig.calls("players/cmd/group_volume") == [{"player_id": QID, "volume_level": 40}]
        assert rig.server.players[QID].volume_level == 40
        assert await rig.backend.get_volume() == 40

    async def test_set_volume_clamps(self, rig: Rig) -> None:
        await rig.backend.set_volume(150)
        await rig.backend.set_volume(-5)
        assert [c["volume_level"] for c in rig.calls("players/cmd/group_volume")] == [100, 0]

    async def test_rapid_changes_coalesce_and_the_latest_wins(self, rig: Rig) -> None:
        await asyncio.gather(*(rig.backend.set_volume(level) for level in (10, 20, 30, 35)))
        await rig.settle()

        levels = [c["volume_level"] for c in rig.calls("players/cmd/group_volume")]
        assert levels[-1] == 35
        assert len(levels) < 4
        assert rig.server.players[QID].volume_level == 35

    async def test_get_volume_returns_our_value_until_ma_caught_up(self, rig: Rig) -> None:
        assert await rig.backend.get_volume() == 30
        rig.server.gate("players/cmd/group_volume")

        first = asyncio.create_task(rig.backend.set_volume_delta(5))
        await rig.settle()
        assert await rig.backend.get_volume() == 35  # MA still says 30
        second = asyncio.create_task(rig.backend.set_volume_delta(5))  # a second button press
        await rig.settle()
        rig.server.release("players/cmd/group_volume")
        assert await first == 35 and await second == 40
        await rig.settle()

        assert rig.server.players[QID].volume_level == 40
        assert rig.cb.volume.call_count == 0

    async def test_get_volume_reads_ma(self, rig: Rig) -> None:
        rig.server.set_player_volume(QID, 66)
        await rig.settle()
        assert await rig.backend.get_volume() == 66

    async def test_volume_without_ma_is_skipped(self, rig: Rig) -> None:
        rig.server.set_reachable(False, drop=True)
        await rig.wait(lambda: not rig.conn.is_connected)
        await rig.backend.set_volume(70)  # no exception
        assert rig.calls("players/cmd/group_volume") == []

    async def test_group_player_reads_group_volume(self, rig: Rig) -> None:
        rig.server.add_player("up2", "Bath", volume=20)
        rig.server.add_player(
            "grp", "Downstairs", type=PlayerType.GROUP, group_members=[QID, "up2"]
        )
        await rig.settle()
        group, _cb = rig.make_backend("grp", "Downstairs (MA)")
        await group.connect()
        rig.server.set_player_volume("up2", 60)  # group_volume follows the members
        await rig.settle()
        grp = rig.server.players["grp"]
        assert grp.group_volume != grp.volume_level

        assert await group.get_volume() == grp.group_volume

        await group.set_volume(50)
        assert rig.calls("players/cmd/group_volume")[-1] == {"player_id": "grp", "volume_level": 50}
        assert rig.server.players["up2"].volume_level == 50

    async def test_device_side_change_is_reported_once_after_the_debounce(
        self, rig: Rig, timing: Callable[..., None]
    ) -> None:
        timing(VOLUME_DEBOUNCE_SECONDS=0.1)
        for level in (55, 60, 65):
            rig.server.set_player_volume(QID, level)
            await rig.settle()
        assert rig.cb.volume.call_count == 0  # still debouncing

        await rig.wait(lambda: rig.cb.volume.called)
        await rig.idle(0.15)
        rig.cb.volume.assert_called_once_with(65)
        assert await rig.backend.get_volume() == 65

    @pytest.mark.parametrize("updates_after_result", [False, True])
    async def test_our_own_change_is_not_echoed(self, rig: Rig, updates_after_result: bool) -> None:
        rig.server.player_updates_after_result = updates_after_result
        await rig.backend.set_volume(40)
        await rig.backend.set_volume(45)
        await rig.idle(0.15)
        assert rig.cb.volume.call_count == 0
        assert rig.server.players[QID].volume_level == 45

    async def test_device_side_change_to_fifty_is_reported(self, rig: Rig) -> None:
        # MA starts at 30; the user drags MA's slider to 50
        rig.server.set_player_volume(QID, 50)
        await rig.wait(lambda: rig.cb.volume.called, timeout=0.5)
        rig.cb.volume.assert_called_once_with(50)


# ---------------------------------------------------------------------- session set-up


class TestSessionSetup:
    async def test_repeat_and_autoplay_are_turned_off_after_the_first_play_media(
        self, rig: Rig
    ) -> None:
        rig.queue().repeat_mode = RepeatMode.ALL
        rig.queue().autoplay_enabled = True

        await rig.play("101")

        names = rig.write_names()
        assert names.index("player_queues/play_media") < names.index("player_queues/repeat")
        assert rig.calls("player_queues/repeat") == [{"queue_id": QID, "repeat_mode": "off"}]
        assert rig.calls("player_queues/autoplay") == [{"queue_id": QID, "autoplay_enabled": False}]
        assert rig.queue().repeat_mode == RepeatMode.OFF
        assert rig.queue().autoplay_enabled is False

        rig.queue().repeat_mode = RepeatMode.ONE  # changed again in MA mid-session
        await rig.play("102")
        assert len(rig.calls("player_queues/repeat")) == 1  # once per selection

    async def test_settings_already_off_send_nothing(self, rig: Rig) -> None:
        await rig.play("101")
        assert rig.calls("player_queues/repeat") == []
        assert rig.calls("player_queues/autoplay") == []

    async def test_prepared_again_after_a_new_selection(self, rig: Rig) -> None:
        rig.queue().repeat_mode = RepeatMode.ALL
        await rig.play("101")
        rig.queue().repeat_mode = RepeatMode.ALL

        rig.backend.prepare_for_selection()
        await rig.play("102")

        assert len(rig.calls("player_queues/repeat")) == 2
        assert rig.queue().repeat_mode == RepeatMode.OFF

    async def test_dynamic_queue_repeat_is_turned_off_after_the_replace(self, rig: Rig) -> None:
        # MA refuses repeat changes while a queue is dynamic; a REPLACE ends dynamic mode
        # (MA 2.10.7 queue_loader). The fake keeps is_dynamic, so emulate MA here.
        server = rig.server
        original = server._commands["player_queues/play_media"]

        def play_media(sock: Any, queue_id: str, media: Any, option: Any = None, **kw: Any) -> Any:
            if option == "replace":
                server.queue(queue_id).is_dynamic = False
            return original(sock, queue_id, media, option, **kw)

        server._commands["player_queues/play_media"] = play_media
        rig.queue().is_dynamic = True
        rig.queue().repeat_mode = RepeatMode.ONE

        await rig.play("101")

        assert rig.queue().repeat_mode == RepeatMode.OFF
        assert await rig.backend.get_state() == PlaybackState.PLAYING

    async def test_refused_repeat_change_does_not_fail_play(self, rig: Rig) -> None:
        rig.queue().is_dynamic = True
        rig.queue().repeat_mode = RepeatMode.ALL

        await rig.play("101")

        assert len(rig.calls("player_queues/repeat")) == 1
        assert await rig.backend.get_state() == PlaybackState.PLAYING

    async def test_autoplay_falls_back_to_dont_stop_the_music(self, rig: Rig) -> None:
        rig.queue().autoplay_enabled = True
        rig.server.fail_next["player_queues/autoplay"] = InvalidCommand(
            "Invalid command: player_queues/autoplay"
        )

        await rig.play("101")

        assert rig.calls("player_queues/dont_stop_the_music") == [
            {"queue_id": QID, "dont_stop_the_music_enabled": False}
        ]
        assert rig.queue().autoplay_enabled is False


# ----------------------------------------------------------------------------- gapless


class TestGapless:
    async def test_arm_waits_for_the_track_to_settle(
        self, rig: Rig, timing: Callable[..., None]
    ) -> None:
        timing(NEXT_ARM_DELAY_SECONDS=0.2)
        await rig.play("101")

        assert await rig.arm("102") is False  # too soon after the start
        assert len(rig.calls("player_queues/play_media")) == 1
        await rig.idle(0.25)

        assert await rig.arm("102", queue_item_id=7) is True
        call = rig.calls("player_queues/play_media")[-1]
        assert (call["media"], call["option"]) == ("qobuz://track/102", "replace_next")
        assert rig.server.track_ids(QID) == ["101", "102"]
        armed = rig.backend._armed
        assert armed is not None
        assert (armed.item_id, armed.track_id, armed.qobuz_queue_item_id) == (
            rig.ma_item_id(1),
            "102",
            7,
        )

    async def test_arm_logs_the_command(self, rig: Rig, caplog: pytest.LogCaptureFixture) -> None:
        await rig.play("101")
        with caplog.at_level(logging.INFO):
            assert await rig.arm("102")
        assert f"[{NAME}] Qobuz: queue next track 102" in caplog.text
        assert "Queued next track 102 in Music Assistant (gapless)" in caplog.text

    async def test_arm_needs_ma_to_be_playing(self, rig: Rig) -> None:
        await rig.play("101")
        await rig.backend.pause()
        commands = len(rig.server.command_log)
        assert await rig.arm("102") is False
        assert len(rig.server.command_log) == commands

    async def test_repeated_arm_of_the_same_track_makes_no_ma_call(self, rig: Rig) -> None:
        await rig.play("101")
        assert await rig.arm("102", queue_item_id=7)
        commands = len(rig.server.command_log)

        for _ in range(3):  # the Qobuz server resends the next track in bursts
            assert await rig.arm("102", queue_item_id=7)

        assert len(rig.server.command_log) == commands
        assert rig.server.track_ids(QID) == ["101", "102"]

    async def test_concurrent_arms_queue_one_track(self, rig: Rig) -> None:
        await rig.play("101")
        results = await asyncio.gather(
            rig.backend.set_next_track(STREAM_URL, meta("102")),
            rig.backend.set_next_track(STREAM_URL, meta("102")),
        )
        await rig.settle()
        assert sorted(results) == [False, True]
        assert (
            len([c for c in rig.calls("player_queues/play_media") if c["option"] == "replace_next"])
            == 1
        )
        assert rig.server.track_ids(QID) == ["101", "102"]

    async def test_no_arm_while_play_is_in_flight(self, rig: Rig) -> None:
        rig.server.gate("player_queues/play_media")
        playing = asyncio.create_task(rig.backend.play(STREAM_URL, meta("101")))
        await rig.settle()

        assert await rig.backend.set_next_track(STREAM_URL, meta("102")) is False
        rig.server.release("player_queues/play_media")
        await playing
        await rig.settle()
        assert len(rig.calls("player_queues/play_media")) == 1

    async def test_no_arm_when_ma_is_not_playing_our_track(self, rig: Rig) -> None:
        assert await rig.arm("102") is False  # nothing played yet
        await rig.play_for("101", 30)
        rig.server.ui_stop(QID)
        await rig.wait(lambda: rig.cb.interrupted.called)
        commands = len(rig.server.command_log)

        assert await rig.arm("102") is False

        assert len(rig.server.command_log) == commands

    async def test_unplayable_next_is_remembered(self, rig: Rig) -> None:
        await rig.play("101")
        rig.server.unplayable_track_ids.add("102")

        assert await rig.arm("102", queue_item_id=7) is False
        commands = len(rig.server.command_log)
        for _ in range(3):  # the Player retries every 0.5 s
            assert await rig.arm("102", queue_item_id=7) is False

        assert len(rig.server.command_log) == commands
        assert rig.backend._armed is None
        assert rig.server.track_ids(QID) == ["101"]

    async def test_other_errors_back_off_then_retry(self, rig: Rig) -> None:
        await rig.play("101")
        rig.server.fail_next["player_queues/play_media"] = PlayerCommandFailed("busy")

        assert await rig.arm("102") is False
        commands = len(rig.server.command_log)
        assert await rig.arm("102") is False  # cooling down: no MA call
        assert len(rig.server.command_log) == commands

        await rig.idle(0.1)
        assert await rig.arm("102") is True
        assert rig.server.track_ids(QID) == ["101", "102"]

    async def test_tracks_the_user_queued_in_ma_are_left_alone(self, rig: Rig) -> None:
        await rig.play("101")
        rig.server.start_playing(QID, ["777"], option="add")  # "Add to queue" in MA's UI
        await rig.settle()
        replace_next_before = len(rig.calls("player_queues/play_media"))

        assert await rig.arm("102") is False

        assert len(rig.calls("player_queues/play_media")) == replace_next_before
        assert rig.server.track_ids(QID) == ["101", "777"]
        assert rig.backend._armed is None

    async def test_next_track_ma_already_has_is_adopted(self, rig: Rig) -> None:
        # Before an add-on restart we had queued 101 + 102; MA plays on and buffers 102
        rig.server.start_playing(QID, ["101", "102"])
        rig.server.buffer_next(QID)
        await rig.settle()
        await rig.play("101")  # adopts what MA plays
        assert rig.calls("player_queues/play_media") == []

        assert await rig.arm("102", queue_item_id=4) is True

        assert rig.calls("player_queues/play_media") == []
        armed = rig.backend._armed
        assert armed is not None and armed.item_id == rig.ma_item_id(1)
        rig.server.finish_current_track(QID)
        await rig.wait(lambda: rig.cb.next_started.called)
        assert rig.cb.endings() == {**NOTHING_FIRED, "next_started": 1}

    async def test_clear_next_track_deletes_the_armed_item(self, rig: Rig) -> None:
        await rig.play("101")
        assert await rig.arm("102")
        item = rig.ma_item_id(1)

        await rig.backend.clear_next_track()
        await rig.settle()

        assert rig.calls("player_queues/delete_item") == [
            {"queue_id": QID, "item_id_or_index": item}
        ]
        assert rig.server.track_ids(QID) == ["101"]
        assert rig.backend._armed is None
        assert item not in rig.backend._stale_item_ids

    async def test_clear_with_nothing_armed_is_a_no_op(self, rig: Rig) -> None:
        await rig.play("101")
        writes = list(rig.server.writes)
        await rig.backend.clear_next_track()
        assert rig.server.writes == writes

    async def test_buffered_next_is_stepped_past_to_the_new_next(self, rig: Rig) -> None:
        await rig.play("101")
        assert await rig.arm("102")
        rig.server.buffer_next(QID)  # the speaker already fetches 102
        await rig.settle()

        await rig.backend.clear_next_track()  # the Qobuz queue changed
        await rig.settle()
        assert rig.server.track_ids(QID) == ["101", "102"]  # MA ignored the delete
        assert await rig.arm("103")
        assert rig.server.track_ids(QID) == ["101", "102", "103"]
        new_next = rig.ma_item_id(2)

        rig.server.finish_current_track(QID)  # MA moves on to the outdated 102...

        await rig.wait(lambda: rig.ma_track() == "103", what="jump to the new next")
        assert rig.calls("player_queues/play_index")[-1]["index"] == new_next
        await rig.wait(lambda: rig.cb.next_started.called)
        await rig.idle(0.1)
        assert rig.cb.endings() == {**NOTHING_FIRED, "next_started": 1}
        assert rig.backend._current is not None and rig.backend._current.item_id == new_next

    async def test_buffered_next_with_nothing_else_stops_and_ends_the_track(self, rig: Rig) -> None:
        await rig.play("101")
        assert await rig.arm("102")
        rig.server.buffer_next(QID)
        await rig.backend.clear_next_track()
        await rig.settle()

        rig.server.finish_current_track(QID)

        await rig.wait(lambda: rig.cb.track_ended.called, what="track_ended")
        await rig.idle(0.1)
        assert len(rig.calls("player_queues/stop")) == 1
        assert rig.ma_state() == MAState.IDLE
        assert rig.cb.endings() == {**NOTHING_FIRED, "track_ended": 1}

    async def test_clear_right_after_the_transition_keeps_the_new_track(self, rig: Rig) -> None:
        await rig.play("101")
        assert await rig.arm("102")
        rig.server.finish_current_track(QID)
        await rig.wait(lambda: rig.cb.next_started.called)

        await rig.backend.clear_next_track()  # the Player clears its old arming
        await rig.settle()

        assert rig.calls("player_queues/delete_item") == []
        assert rig.ma_track() == "102" and rig.ma_state() == MAState.PLAYING
        assert rig.backend._current is not None and rig.backend._current.track_id == "102"
        assert await rig.backend.get_state() == PlaybackState.PLAYING
