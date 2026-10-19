"""
Audio backend for one Music Assistant player or group.

The bridge asks MA to play ``qobuz://track/<id>`` on the player's MA queue
(queue id == player id) and MA streams it with its own Qobuz provider, so MA
shows the track, artwork, queue and progress. The streaming URL the Player
looks up is not used; that lookup still checks the track is available.

Ownership:

- The MA queue items this backend created are "ours" (kept across add-on
  restarts in the settings file).
- While *attached* (the Qobuz app plays through us), MA moving onto our queued
  next item is a gapless transition, MA finishing our track is a track end,
  and MA stopping it is an interruption (the app shows stopped). After a stop,
  an interruption or a restart the backend is detached: it follows MA and
  never makes the Player advance.
- Anything else on the queue while attached means someone took the player
  over in MA: the backend lets go and sends MA nothing more until the Qobuz
  app selects the device again (no fight-back).

MA pushes its state over the websocket, so get_state() and get_position()
read the client's cache and never call MA.
"""

import asyncio
import logging
import time
from dataclasses import asdict, dataclass
from typing import Any, Awaitable, Callable, Optional

from music_assistant_models.enums import EventType, PlayerType, QueueOption, RepeatMode
from music_assistant_models.enums import PlaybackState as MAPlaybackState
from music_assistant_models.errors import (
    InvalidDataError,
    InvalidProviderURI,
    MediaNotFoundError,
    MusicAssistantError,
    PlayerUnavailableError,
    UnplayableMediaError,
)

from qobuz_proxy.backends.base import AudioBackend, ExternalPlaybackAware, TrackUnavailableError
from qobuz_proxy.backends.types import BackendTrackMetadata, PlaybackState

from .connection import ConnectionState, ConnectionStatus, NotConnectedError

logger = logging.getLogger(__name__)

# Ignore MA's transitional states this long after we started or moved a track.
START_GRACE_SECONDS = 4.0
# MA "idle" must last this long before it counts (streams restart briefly on seek).
IDLE_SETTLE_SECONDS = 3.0
# An MA stop/next/seek marks the queue busy; an idle this soon after one is not a track end.
PLAY_ACTION_WINDOW_SECONDS = 3.0
# A stop with more than this left is not the track ending by itself.
END_MARGIN_MS = 10_000
# MA still reports "playing" this long past the track's end: it lost track of the speaker.
STALL_MARGIN_MS = 10_000
# ...and keeps doing so this long (right after a seek MA briefly adds the old stream's time).
STALL_CONFIRM_SECONDS = 5.0
# After "next" in MA, MA shows the track we left again (at its old position) until the
# speaker starts the next one; for this long that is not a "previous".
PROMOTE_SETTLE_SECONDS = 10.0
# Tell the app the track stopped if MA stays unreachable this long while playing.
DISCONNECT_GRACE_SECONDS = 20.0
# Seeks this close to where MA already is are skipped (MA restarts the stream to seek).
SEEK_TOLERANCE_MS = 1_500
# Right after taking over what MA plays, a seek back by up to this much is a stale position.
ADOPT_STALE_SEEK_MS = 300_000
# Don't take over MA's current track right after we stopped it ourselves.
ADOPT_BLOCK_AFTER_STOP_SECONDS = 5.0
# Wait this long after a track starts before queuing the next one in MA.
NEXT_ARM_DELAY_SECONDS = 1.5
# Wait before retrying to queue a next track after an MA error.
NEXT_RETRY_COOLDOWN_SECONDS = 5.0
# Don't try to queue a next track for this long when the user queued their own in MA.
FOREIGN_NEXT_COOLDOWN_SECONDS = 30.0
# Keep the queued next track this long for play() after stop(next_track_id=...).
JUMP_WAIT_SECONDS = 3.0
# After our pause/resume/play, wait this long for MA to report it before trusting MA's state
# again (a real speaker can take seconds to confirm, and MA keeps reporting the old state).
EXPECT_STATE_SECONDS = 10.0
# Coalesce device-side volume changes; ignore MA's echoes this long after we set one.
VOLUME_DEBOUNCE_SECONDS = 0.3
VOLUME_SETTLE_SECONDS = 1.5
# Starting at or below this position is a normal start, not a positioned one.
POSITIONED_START_MIN_MS = 2_000
# MA can take a while to resolve, buffer and start a track on a speaker or group.
PLAY_TIMEOUT_SECONDS = 30.0

_UNPLAYABLE_ERRORS = (MediaNotFoundError, UnplayableMediaError, InvalidProviderURI)
_PLAYING_OR_PAUSED = (MAPlaybackState.PLAYING, MAPlaybackState.PAUSED)


@dataclass
class _OwnItem:
    """An MA queue item this backend created."""

    item_id: str  # MA queue_item_id
    track_id: str  # Qobuz track id
    duration_ms: int
    title: str = ""
    qobuz_queue_item_id: int = 0  # Qobuz queue occurrence (queued next track only)


def _format_ms(ms: int) -> str:
    seconds = max(0, ms) // 1000
    return f"{seconds // 60}:{seconds % 60:02d}"


def _uri(track_id: str) -> str:
    return f"qobuz://track/{track_id}"


def _is_track(item: Any, track_id: str) -> bool:
    """Whether an MA queue item is this Qobuz track (MA library items included)."""
    media = item.media_item
    if media is None:
        return False
    if (media.provider == "qobuz" or media.provider.startswith("qobuz--")) and str(
        media.item_id
    ) == track_id:
        return True
    return any(
        m.provider_domain == "qobuz" and str(m.item_id) == track_id for m in media.provider_mappings
    )


def _read_volume(player: Any) -> Optional[int]:
    """A group (or a speaker leading one) has a group volume; a single speaker its own."""
    grouped = bool(player.group_members) or player.type == PlayerType.GROUP
    value = player.group_volume if grouped and player.group_volume is not None else None
    if value is None:
        value = player.volume_level
    return int(value) if value is not None else None


class MusicAssistantBackend(AudioBackend, ExternalPlaybackAware):
    """Audio backend for one Music Assistant player or group."""

    def __init__(
        self,
        connection: Any,
        player_id: str,
        name: str,
        session: Optional[dict[str, Any]] = None,
        save_session: Callable[[Optional[dict[str, Any]]], None] = lambda session: None,
    ) -> None:
        super().__init__(name=name)
        self._conn = connection
        self._player_id = player_id
        self._saved_session = session
        self._save_session_cb = save_session

        self._current: Optional[_OwnItem] = None
        self._current_meta: Optional[BackendTrackMetadata] = None
        self._armed: Optional[_OwnItem] = None  # our next track, already queued in MA
        self._own_item_ids: set[str] = set()
        self._stale_item_ids: set[str] = set()  # queued next items the app no longer wants

        self._attached = False  # the Qobuz app is playing through us right now
        self._external = False  # someone else took the player over in MA
        self._busy = 0  # our own multi-step MA writes in flight
        self._arming = False
        self._play_started_at = 0.0
        self._last_play_action_at = 0.0
        self._last_stop_at: Optional[float] = None
        self._ma_state: Optional[MAPlaybackState] = None
        self._paused_hold = False
        self._end_reported = False
        self._position_ms = 0
        self._position_at = 0.0  # time.monotonic() of the last position update
        self._furthest_position_ms = 0
        self._seen_playing = False  # MA reported our current item as playing
        self._in_ma_event = False  # handling an MA event (not a poll or a timer)
        self._armed_restored = False  # _armed came from the settings file
        self._last_seek_target: Optional[int] = None
        self._last_seek_at = 0.0
        self._adopt_seek_until = 0.0
        self._adopt_position_ms = 0
        self._session_prepared = False
        self._expected_state: Optional[MAPlaybackState] = None
        self._expected_until = 0.0
        self._stall_since: Optional[float] = None
        self._left_item_id: Optional[str] = None  # current item before the last promotion
        self._promoted_at = 0.0

        self._failed_next: set[tuple[str, int]] = set()
        self._next_retry_after = 0.0

        self._pending_volume: Optional[int] = None
        self._volume_lock = asyncio.Lock()
        self._volume_settle_until = 0.0
        self._last_ma_volume: Optional[int] = None

        self._timers: dict[str, asyncio.TimerHandle] = {}
        self._on_external_playback_cb: Optional[Callable[[], Awaitable[None]]] = None
        self._takeover_task: Optional["asyncio.Future[Any]"] = None
        self._stale_task: Optional["asyncio.Future[Any]"] = None
        self._tasks: set["asyncio.Future[Any]"] = set()

    @property
    def player_id(self) -> str:
        return self._player_id

    @property
    def supports_gapless(self) -> bool:
        return True

    @property
    def reports_external_resume(self) -> bool:
        return True

    async def connect(self) -> bool:
        """Listen to this player's MA events.

        Succeeds while MA is unreachable too: the Qobuz device stays visible
        and commands fail with a clear error until the connection is back.
        """
        if self._is_connected:
            return True
        self._restore_session()
        self._conn.add_event_listener(self._player_id, self._on_ma_event)
        self._conn.add_status_listener(self._on_connection_status)
        self._is_connected = True
        self._sync_volume_from_ma()
        logger.info("[%s] Music Assistant backend ready (player %s)", self.name, self._player_id)
        return True

    async def disconnect(self) -> None:
        """Stop listening. MA keeps playing: an add-on restart must not stop the music."""
        if not self._is_connected:
            return
        self._is_connected = False
        self._conn.remove_event_listener(self._player_id, self._on_ma_event)
        self._conn.remove_status_listener(self._on_connection_status)
        for timer in self._timers.values():
            timer.cancel()
        self._timers.clear()
        for task in list(self._tasks):
            task.cancel()

    async def play(self, url: str, metadata: BackendTrackMetadata) -> None:
        """Play a Qobuz track through Music Assistant (``url`` is not used)."""
        start_ms = metadata.start_position_ms
        logger.info(
            "[%s] Qobuz: play track %s '%s' – %s%s",
            self.name,
            metadata.track_id,
            metadata.title,
            metadata.artist,
            f" from {_format_ms(start_ms)}" if start_ms >= POSITIONED_START_MIN_MS else "",
        )
        if not self._conn.is_connected:
            raise NotConnectedError("Music Assistant is not connected")
        if self._external:
            raise RuntimeError(
                "Another source is playing on this player in Music Assistant; "
                "select it again in the Qobuz app"
            )
        self._cancel_timer("jump")
        self._cancel_timer("idle")
        adopted = False
        self._busy += 1
        try:
            item = await self._adopt(metadata)
            adopted = item is not None
            if item is None:
                item = await self._jump_to_armed(metadata, start_ms)
            if item is None:
                item = await self._start_track(metadata, start_ms)
            self._set_current(item, metadata)
            if adopted:
                self._position_ms = self._furthest_position_ms = self._adopt_position_ms
            elif start_ms >= POSITIONED_START_MIN_MS:
                self._position_ms = self._furthest_position_ms = start_ms
            self._attached = True
            self._notify_state_change(PlaybackState.PLAYING)
        except _UNPLAYABLE_ERRORS as e:
            logger.error(
                "[%s] Music Assistant could not play Qobuz track %s ('%s'): %s. Is Qobuz set "
                "up in Music Assistant with an account that can play this track?",
                self.name,
                metadata.track_id,
                metadata.title,
                e,
            )
            raise TrackUnavailableError(
                f"Music Assistant could not play track {metadata.track_id}"
            ) from e
        except PlayerUnavailableError as e:
            logger.error(
                "[%s] Music Assistant reports this player as unavailable: %s", self.name, e
            )
            raise RuntimeError("The Music Assistant player is unavailable") from e
        except MusicAssistantError as e:
            raise RuntimeError(f"Music Assistant refused to play: {e}") from e
        finally:
            self._busy -= 1
        # The Player records PLAYING and the start position once we return; look at
        # MA's state (and report MA's real position after an adoption) after that.
        loop = asyncio.get_running_loop()
        if adopted:
            loop.call_soon(self._notify_position_update, self._position_ms)
        loop.call_soon(self._evaluate)

    async def _adopt(self, metadata: BackendTrackMetadata) -> Optional[_OwnItem]:
        """Continue MA's current item if it already plays this track.

        After an add-on restart (or a new selection) the Qobuz app asks for the
        track MA is still playing; restarting it would be audible.
        """
        if (
            self._last_stop_at is not None
            and time.monotonic() - self._last_stop_at < ADOPT_BLOCK_AFTER_STOP_SECONDS
        ):
            return None
        queue = self._conn.queue(self._player_id)
        if queue is None or not queue.active or queue.current_item is None:
            return None
        if queue.state not in _PLAYING_OR_PAUSED:
            return None
        cur = queue.current_item
        if not _is_track(cur, metadata.track_id):
            return None
        if self._current is not None and cur.queue_item_id != self._current.item_id:
            return None
        logger.info(
            "[%s] Music Assistant is already playing track %s; continuing it without restarting",
            self.name,
            metadata.track_id,
        )
        if queue.state == MAPlaybackState.PAUSED:
            qid = self._player_id
            await self._conn.call(
                f"player_queues/play queue={qid} (resume the track MA has)",
                lambda c: c.player_queues.play(qid),
            )
        self._adopt_position_ms = int(queue.corrected_elapsed_time * 1000)
        self._adopt_seek_until = time.monotonic() + START_GRACE_SECONDS
        return self._own_item(cur, metadata)

    async def _jump_to_armed(
        self, metadata: BackendTrackMetadata, start_ms: int
    ) -> Optional[_OwnItem]:
        """Play the next track MA already has queued (a "next" in the Qobuz app)."""
        armed = self._armed
        if armed is None or armed.track_id != metadata.track_id:
            return None
        qid = self._player_id
        seek_s = start_ms // 1000 if start_ms >= POSITIONED_START_MIN_MS else 0
        try:
            await self._conn.call(
                f"player_queues/play_index queue={qid} item={armed.item_id}",
                lambda c: c.player_queues.play_index(qid, armed.item_id, seek_position=seek_s),
                timeout=PLAY_TIMEOUT_SECONDS,
            )
        except InvalidDataError:
            self._armed = None  # the queued item is gone (queue edited in MA): start afresh
            return None
        self._armed = None
        return armed

    async def _start_track(self, metadata: BackendTrackMetadata, start_ms: int) -> _OwnItem:
        """Replace the MA queue with this track, optionally starting mid-track."""
        uri = _uri(metadata.track_id)
        qid = self._player_id
        if self._armed is not None:
            self._stale_item_ids.add(self._armed.item_id)
            self._armed = None
        found: Optional[Any] = None
        if start_ms < POSITIONED_START_MIN_MS:
            await self._conn.call(
                f"player_queues/play_media queue={qid} media={uri} option=replace",
                lambda c: c.player_queues.play_media(qid, media=uri, option=QueueOption.REPLACE),
                timeout=PLAY_TIMEOUT_SECONDS,
            )
            queue = self._conn.queue(qid)
            current = queue.current_item if queue is not None else None
            if (
                current is not None
                and current.queue_item_id not in self._own_item_ids
                and _is_track(current, metadata.track_id)
            ):
                found = current
            else:
                found = await self._find_new_item(metadata.track_id, offset=0)
        else:
            # One stream start at the right spot instead of play-then-seek
            await self._conn.call(
                f"player_queues/clear queue={qid}", lambda c: c.player_queues.clear(qid)
            )
            await self._conn.call(
                f"player_queues/play_media queue={qid} media={uri} option=add",
                lambda c: c.player_queues.play_media(qid, media=uri, option=QueueOption.ADD),
                timeout=PLAY_TIMEOUT_SECONDS,
            )
            found = await self._find_new_item(metadata.track_id, offset=0)
            if found is not None:
                seek_s = start_ms // 1000
                item_id = found.queue_item_id
                await self._conn.call(
                    f"player_queues/play_index queue={qid} item={item_id} seek={seek_s}s",
                    lambda c: c.player_queues.play_index(qid, item_id, seek_position=seek_s),
                    timeout=PLAY_TIMEOUT_SECONDS,
                )
        if found is None:
            raise MediaNotFoundError(
                f"Music Assistant did not queue Qobuz track {metadata.track_id}"
            )
        # Only after the queue was replaced: a dynamic queue refuses repeat changes before
        await self._prepare_session()
        return self._own_item(found, metadata)

    async def _prepare_session(self) -> None:
        """Turn off MA queue settings that would fight the Qobuz queue (once per selection)."""
        if self._session_prepared:
            return
        queue = self._conn.queue(self._player_id)
        if queue is None:
            return
        self._session_prepared = True
        qid = self._player_id
        try:
            if queue.repeat_mode != RepeatMode.OFF:
                await self._conn.call(
                    f"player_queues/repeat queue={qid} repeat_mode=off",
                    lambda c: c.player_queues.repeat(qid, RepeatMode.OFF),
                )
                logger.info(
                    "[%s] Turned off repeat in Music Assistant so the Qobuz queue decides "
                    "what plays next",
                    self.name,
                )
        except (NotConnectedError, MusicAssistantError) as e:
            logger.info("[%s] Could not turn off repeat in Music Assistant: %s", self.name, e)
        try:
            if queue.autoplay_enabled:
                await self._conn.call(
                    f"player_queues/autoplay queue={qid} autoplay_enabled=false",
                    lambda c: c.send_command(
                        "player_queues/autoplay", queue_id=qid, autoplay_enabled=False
                    ),
                )
                logger.info(
                    "[%s] Turned off autoplay in Music Assistant so the album ends when the "
                    "Qobuz queue ends",
                    self.name,
                )
        except (NotConnectedError, MusicAssistantError) as e:
            logger.info("[%s] Could not turn off autoplay in Music Assistant: %s", self.name, e)

    async def pause(self) -> None:
        logger.info("[%s] Qobuz: pause", self.name)
        if not self._conn.is_connected:
            raise NotConnectedError("Music Assistant is not connected")
        self._cancel_timer("idle")
        if self._ma_plays_ours():
            qid = self._player_id
            await self._conn.call(
                f"player_queues/pause queue={qid}", lambda c: c.player_queues.pause(qid)
            )
            self._expect(MAPlaybackState.PAUSED)
        self._paused_hold = True
        self._notify_state_change(PlaybackState.PAUSED)

    async def resume(self) -> bool:
        logger.info("[%s] Qobuz: resume", self.name)
        if not self._conn.is_connected or self._external or self._current is None:
            return False
        current = self._current
        qid = self._player_id
        queue = self._conn.queue(qid)
        if (
            queue is not None
            and queue.active
            and queue.current_item is not None
            and queue.current_item.queue_item_id == current.item_id
        ):
            # MA's queue state lags the speaker by ~0.5 s: right after our own pause
            # (e.g. the re-pause after a seek) the queue still says playing
            player = self._conn.player(qid)
            speaker_paused = player is not None and player.playback_state == MAPlaybackState.PAUSED
            if queue.state == MAPlaybackState.PLAYING and not speaker_paused:
                self._resumed()
                return True
            if queue.state in _PLAYING_OR_PAUSED or (
                queue.state == MAPlaybackState.IDLE and not queue.ended
            ):
                self._busy += 1
                try:
                    await self._conn.call(
                        f"player_queues/play queue={qid}", lambda c: c.player_queues.play(qid)
                    )
                except (NotConnectedError, MusicAssistantError):
                    return False
                finally:
                    self._busy -= 1
                self._resumed()
                return True
        if queue is not None and self._foreign_content_playing(queue):
            self._takeover("Music Assistant is playing something else")
            return False
        # Our item is gone or finished (e.g. MA cleared it): start it again where we were
        if self._current_meta is None:
            return False
        meta = BackendTrackMetadata(
            **{**asdict(self._current_meta), "start_position_ms": self._position_ms}
        )
        self._current = None
        try:
            await self.play("", meta)
        except Exception as e:
            logger.warning("[%s] Could not resume in Music Assistant: %s", self.name, e)
            return False
        return True

    def _resumed(self) -> None:
        self._paused_hold = False
        self._attached = True
        self._ma_state = MAPlaybackState.PLAYING
        self._play_started_at = time.monotonic()
        self._expect(MAPlaybackState.PLAYING)
        self._notify_state_change(PlaybackState.PLAYING)
        asyncio.get_running_loop().call_soon(self._evaluate)

    async def stop(self, *, next_track_id: Optional[str] = None) -> None:
        self._cancel_timer("idle")
        armed = self._armed
        self._attached = False
        self._paused_hold = False
        if next_track_id is not None and armed is not None and armed.track_id == next_track_id:
            # play() is about to jump straight to the item MA already has queued.
            # If it doesn't come (load superseded, skip while paused), stop then.
            logger.info("[%s] Qobuz: stop (switching to the queued next track)", self.name)
            self._set_timer("jump", JUMP_WAIT_SECONDS, self._on_jump_timeout)
            self._notify_state_change(PlaybackState.STOPPED)
            return
        logger.info("[%s] Qobuz: stop", self.name)
        self._cancel_timer("jump")
        await self._stop_ma()
        if armed is not None:
            self._stale_item_ids.add(armed.item_id)
        self._armed = None
        self._notify_state_change(PlaybackState.STOPPED)

    async def _stop_ma(self) -> None:
        """Stop MA, but only while it still plays our track."""
        if not self._ma_plays_ours():
            return
        qid = self._player_id
        self._busy += 1
        try:
            await self._conn.call(
                f"player_queues/stop queue={qid}", lambda c: c.player_queues.stop(qid)
            )
            self._last_stop_at = time.monotonic()
            self._ma_state = MAPlaybackState.IDLE
        except (NotConnectedError, MusicAssistantError) as e:
            logger.warning("[%s] Music Assistant did not stop: %s", self.name, e)
        finally:
            self._busy -= 1

    def _on_jump_timeout(self) -> None:
        if self._attached:
            return
        if self._armed is not None:
            self._stale_item_ids.add(self._armed.item_id)
            self._armed = None
        logger.info("[%s] The queued next track was not played; stopping", self.name)
        self._spawn(self._stop_ma())

    async def seek(self, position_ms: int) -> None:
        now = time.monotonic()
        live = self._live_position_ms()
        if now < self._adopt_seek_until and live - ADOPT_STALE_SEEK_MS <= position_ms <= (
            live + SEEK_TOLERANCE_MS
        ):
            logger.info(
                "[%s] Qobuz: seek to %s skipped: Music Assistant kept playing at %s",
                self.name,
                _format_ms(position_ms),
                _format_ms(live),
            )
            return
        if (
            self._last_seek_target is not None
            and now - self._last_seek_at < 3.0
            and abs(position_ms - self._last_seek_target) <= SEEK_TOLERANCE_MS
        ) or (self._our_queue() is not None and abs(position_ms - live) <= SEEK_TOLERANCE_MS):
            logger.debug("[%s] Already at %s; skipping seek", self.name, _format_ms(position_ms))
            return
        logger.info("[%s] Qobuz: seek to %s", self.name, _format_ms(position_ms))
        if not self._conn.is_connected:
            raise NotConnectedError("Music Assistant is not connected")
        if not self._ma_plays_ours():
            raise RuntimeError("Cannot seek: Music Assistant is not playing this Qobuz track")
        qid = self._player_id
        seconds = int(round(position_ms / 1000))
        was_paused = self._paused_hold
        self._cancel_timer("idle")
        self._busy += 1
        try:
            await self._conn.call(
                f"player_queues/seek queue={qid} position={seconds}s",
                lambda c: c.player_queues.seek(qid, seconds),
                timeout=PLAY_TIMEOUT_SECONDS,
            )
            if was_paused:
                # MA starts playing when a paused queue seeks; keep it paused
                await self._conn.call(
                    f"player_queues/pause queue={qid} (stay paused after seek)",
                    lambda c: c.player_queues.pause(qid),
                )
        finally:
            self._busy -= 1
        self._expect(MAPlaybackState.PAUSED if was_paused else MAPlaybackState.PLAYING)
        # MA restarts the stream to seek; don't read that as a stop
        self._play_started_at = time.monotonic()
        self._last_seek_target = position_ms
        self._last_seek_at = time.monotonic()
        self._position_ms = position_ms
        self._position_at = self._last_seek_at
        self._furthest_position_ms = position_ms

    async def get_position(self) -> int:
        queue = self._our_queue()
        if queue is not None:
            self._update_position(queue)
        return self._position_ms

    def _live_position_ms(self) -> int:
        queue = self._our_queue()
        if queue is None:
            return self._position_ms
        return int(queue.corrected_elapsed_time * 1000)

    async def set_volume(self, level: int) -> None:
        level = max(0, min(100, level))
        if self._external:
            logger.info("[%s] Qobuz: volume %s%% ignored (not our player now)", self.name, level)
            return
        self._volume = level
        self._pending_volume = level
        self._volume_settle_until = time.monotonic() + VOLUME_SETTLE_SECONDS
        async with self._volume_lock:
            # A newer value queued behind us wins; only send the latest one.
            if self._pending_volume != level:
                return
            self._pending_volume = None
            if not self._conn.is_connected:
                logger.warning(
                    "[%s] Qobuz: volume %s%% not applied: Music Assistant is not connected",
                    self.name,
                    level,
                )
                return
            logger.info("[%s] Qobuz: volume %s%%", self.name, level)
            self._last_ma_volume = level
            pid = self._player_id
            # group_volume sets a group (or a speaker leading one) as a whole and
            # falls back to a plain volume set for a single speaker.
            await self._conn.call(
                f"players/cmd/group_volume player={pid} level={level}",
                lambda c: c.players.group_volume(pid, level),
            )
            self._volume_settle_until = time.monotonic() + VOLUME_SETTLE_SECONDS

    async def get_volume(self) -> int:
        if time.monotonic() < self._volume_settle_until:
            return self._volume  # MA has not caught up with our last change yet
        player = self._conn.player(self._player_id)
        level = _read_volume(player) if player is not None else None
        if level is not None:
            self._volume = level
        return self._volume

    def _sync_volume_from_ma(self) -> None:
        """Start from MA's level, so a later change in MA is not taken for our own echo."""
        player = self._conn.player(self._player_id)
        level = _read_volume(player) if player is not None else None
        if level is not None:
            self._volume = self._last_ma_volume = level

    async def get_state(self) -> PlaybackState:
        # The Player polls this every 0.5 s while playing or paused, which also
        # catches decisions a quiet MA (no further events) would leave pending.
        self._evaluate()
        if self._busy:
            return PlaybackState.PLAYING if self._state == PlaybackState.STOPPED else self._state
        if self._current is None or not self._attached:
            return PlaybackState.STOPPED
        if not self._conn.is_connected:
            return self._state  # frozen until MA is back
        queue = self._our_queue()
        if queue is None:
            return self._state
        expected = self._pending_expectation()
        if expected is not None and queue.state != expected:
            # MA has not caught up with our last command yet
            return (
                PlaybackState.PAUSED
                if expected == MAPlaybackState.PAUSED
                else PlaybackState.PLAYING
            )
        if queue.state == MAPlaybackState.PLAYING:
            return PlaybackState.PLAYING
        if queue.state == MAPlaybackState.PAUSED or self._paused_hold:
            return PlaybackState.PAUSED
        # Idle but not decided yet (settling, or a speaker still starting)
        if "idle" in self._timers or self._in_grace():
            return PlaybackState.PLAYING
        return PlaybackState.STOPPED

    async def set_next_track(
        self, url: str, metadata: BackendTrackMetadata, queue_item_id: int = 0
    ) -> bool:
        """Queue the next Qobuz track right after the current one in MA."""
        key = (metadata.track_id, queue_item_id)
        armed = self._armed
        if (
            armed is not None
            and armed.track_id == metadata.track_id
            and armed.item_id not in self._stale_item_ids
        ):
            armed.qobuz_queue_item_id = queue_item_id
            self._armed_restored = False
            return True
        now = time.monotonic()
        if key in self._failed_next or now < self._next_retry_after:
            return False
        if self._arming or self._busy or not self._attached or not self._ma_plays_ours():
            return False
        qid = self._player_id
        queue = self._conn.queue(qid)
        if queue is None or queue.state != MAPlaybackState.PLAYING:
            return False  # MA only hands the next track to a playing speaker
        if queue.extra_attributes.get("play_action_in_progress") or (
            now - self._play_started_at < NEXT_ARM_DELAY_SECONDS
        ):
            return False
        current_index = queue.current_index or 0
        uri = _uri(metadata.track_id)
        self._arming = True
        try:
            after = await self._conn.call(
                f"player_queues/items queue={qid} offset={current_index + 1}",
                lambda c: c.player_queues.get_queue_items(qid, limit=10, offset=current_index + 1),
            )
            first = after[0] if after else None
            if (
                first is not None
                and _is_track(first, metadata.track_id)
                and (
                    first.queue_item_id in self._own_item_ids
                    or queue.index_in_buffer == current_index + 1
                )
            ):
                # MA already has this track next (e.g. queued before an add-on restart)
                self._arm(self._own_item(first, metadata), queue_item_id)
                return True
            if any(i.queue_item_id not in self._own_item_ids for i in after):
                logger.info(
                    "[%s] Music Assistant has other tracks queued after this one; leaving "
                    "them alone (Music Assistant takes over when this track ends)",
                    self.name,
                )
                self._next_retry_after = now + FOREIGN_NEXT_COOLDOWN_SECONDS
                return False
            logger.info(
                "[%s] Qobuz: queue next track %s '%s'", self.name, metadata.track_id, metadata.title
            )
            if armed is not None:
                # REPLACE_NEXT drops it unless the speaker already buffered it
                self._stale_item_ids.add(armed.item_id)
                self._armed = None
            await self._conn.call(
                f"player_queues/play_media queue={qid} media={uri} option=replace_next",
                lambda c: c.player_queues.play_media(
                    qid, media=uri, option=QueueOption.REPLACE_NEXT
                ),
                timeout=PLAY_TIMEOUT_SECONDS,
            )
            found = await self._find_new_item(metadata.track_id, offset=current_index)
            if found is None:
                raise MediaNotFoundError(f"Music Assistant did not queue track {metadata.track_id}")
            self._arm(self._own_item(found, metadata), queue_item_id)
            logger.info(
                "[%s] Queued next track %s in Music Assistant (gapless)",
                self.name,
                metadata.track_id,
            )
            return True
        except _UNPLAYABLE_ERRORS as e:
            self._failed_next.add(key)
            logger.warning(
                "[%s] Music Assistant cannot queue track %s: %s (it will be handled when the "
                "current track ends)",
                self.name,
                metadata.track_id,
                e,
            )
            return False
        except (NotConnectedError, MusicAssistantError) as e:
            self._next_retry_after = time.monotonic() + NEXT_RETRY_COOLDOWN_SECONDS
            logger.warning("[%s] Could not queue the next track: %s", self.name, e)
            return False
        finally:
            self._arming = False
            # Runs after the Player recorded the queued track
            asyncio.get_running_loop().call_soon(self._evaluate)

    def _arm(self, item: _OwnItem, queue_item_id: int) -> None:
        item.qobuz_queue_item_id = queue_item_id
        self._armed = item
        self._armed_restored = False
        self._save_session()

    async def clear_next_track(self) -> None:
        armed = self._armed
        if armed is None:
            return
        if self._armed_restored:
            # The Player's first clear after an add-on restart only resets its own (empty)
            # gapless state. Keep MA's item: set_next_track() takes it over if it still fits,
            # and REPLACE_NEXT replaces it if not.
            self._armed_restored = False
            return
        self._armed = None
        # If MA reaches it anyway (already buffered), we step past it
        self._stale_item_ids.add(armed.item_id)
        self._save_session()
        if not self._conn.is_connected or self._external:
            return
        qid = self._player_id
        logger.info(
            "[%s] Qobuz: next track changed; removing %s from MA", self.name, armed.track_id
        )
        self._busy += 1
        try:
            await self._conn.call(
                f"player_queues/delete_item queue={qid} item={armed.item_id}",
                lambda c: c.player_queues.delete_item(qid, armed.item_id),
            )
            if not await self._item_in_queue(armed.item_id):
                self._stale_item_ids.discard(armed.item_id)
        except (NotConnectedError, MusicAssistantError) as e:
            logger.debug("[%s] Could not remove the queued next track: %s", self.name, e)
        finally:
            self._busy -= 1
            asyncio.get_running_loop().call_soon(self._evaluate)

    def on_external_playback(self, callback: Callable[[], Awaitable[None]]) -> None:
        self._on_external_playback_cb = callback

    def prepare_for_selection(self) -> None:
        """A new selection in the Qobuz app may take the player back."""
        self._session_prepared = False
        if self._external:
            self._external = False
            self._current = None
            self._current_meta = None
            self._armed = None

    async def check_external_playback(self) -> bool:
        self._evaluate()
        task = self._takeover_task
        if task is not None and not task.done():
            # Release the old session before a new selection builds on it
            await asyncio.gather(task, return_exceptions=True)
        return self._external

    async def can_apply_remote_state(self) -> bool:
        """Refuse replayed Qobuz snapshots while MA plays something else."""
        if self._external:
            return False
        if self._current is None or not self._conn.is_connected:
            return True
        if self._in_grace() or self._ma_plays_ours():
            return True
        queue = self._conn.queue(self._player_id)
        return queue is None or not self._foreign_content_playing(queue)

    def _on_ma_event(self, event: Any) -> None:
        kind = event.event
        if kind == EventType.PLAYER_UPDATED:
            self._check_device_volume()
        elif kind == EventType.QUEUE_TIME_UPDATED:
            queue = self._our_queue()
            if queue is not None:
                self._update_position(queue, report_jumps=True)
        elif kind == EventType.PLAYER_REMOVED:
            self._start_disconnect_timer()
        elif kind in (EventType.PLAYER_ADDED, EventType.QUEUE_ADDED):
            if self._conn.queue(self._player_id) is not None:
                self._cancel_timer("disconnect")
        self._in_ma_event = True
        try:
            self._evaluate()
        finally:
            self._in_ma_event = False

    def _on_connection_status(self, status: ConnectionStatus) -> None:
        if status.state != ConnectionState.CONNECTED:
            self._start_disconnect_timer()
            return
        self._sync_volume_from_ma()
        # MA may not have registered the player again yet after a restart
        if self._conn.queue(self._player_id) is not None:
            self._cancel_timer("disconnect")
        self._evaluate()

    def _start_disconnect_timer(self) -> None:
        if (
            self._current is None
            or not self._attached
            or self._state != PlaybackState.PLAYING
            or "disconnect" in self._timers
        ):
            return
        self._set_timer("disconnect", DISCONNECT_GRACE_SECONDS, self._on_disconnect_grace_expired)

    def _on_disconnect_grace_expired(self) -> None:
        if not self._attached or self._current is None or self._external:
            return
        if self._conn.is_connected and self._conn.queue(self._player_id) is not None:
            self._evaluate()
            return
        logger.warning(
            "[%s] Lost Music Assistant; showing the track as stopped in the Qobuz app", self.name
        )
        self._report_interrupted()

    def _check_device_volume(self) -> None:
        player = self._conn.player(self._player_id)
        if player is None:
            return
        level = _read_volume(player)
        if level is None or level == self._last_ma_volume:
            return
        self._last_ma_volume = level
        if time.monotonic() < self._volume_settle_until or abs(level - self._volume) <= 1:
            return  # our own change echoed back
        self._volume = level
        self._set_timer("volume", VOLUME_DEBOUNCE_SECONDS, self._report_device_volume)

    def _report_device_volume(self) -> None:
        if self._external:
            return
        logger.info("[%s] MA: volume changed to %s%% in Music Assistant", self.name, self._volume)
        self._notify_volume_change(self._volume)

    def _evaluate(self) -> None:
        """Map MA's queue state onto the Player callbacks (sync; never awaits)."""
        if (
            self._busy
            or self._current is None
            or self._external
            or not self._is_connected
            or not self._conn.is_connected
        ):
            return
        queue = self._conn.queue(self._player_id)
        if queue is None:
            return
        if queue.extra_attributes.get("play_action_in_progress"):
            # MA is switching tracks or handling a stop/next/seek: wait for the
            # settled state, and remember it so a following idle isn't a track end.
            self._last_play_action_at = time.monotonic()
            self._schedule_reevaluate(1.0)
            return
        cur_item = queue.current_item
        cur_id = cur_item.queue_item_id if cur_item is not None else None
        in_grace = self._in_grace()

        if cur_id == self._current.item_id:
            self._evaluate_current(queue, in_grace)
            return

        armed = self._armed
        if armed is not None and cur_id == armed.item_id:
            self._promote_armed(armed, queue)
            return

        if cur_id is not None and cur_id in self._stale_item_ids:
            if self._attached:
                self._step_past_stale(cur_id)
            return

        if in_grace or self._arming:
            self._schedule_reevaluate(self._grace_left() + 0.2)
            return

        if not self._attached:
            # Nothing of ours is live in the Qobuz app: just follow MA
            if cur_id is None or cur_id not in self._own_item_ids or not queue.active:
                logger.debug("[%s] MA moved on to something else", self.name)
                self._forget_current()
            return

        if cur_id is None:
            logger.info("[%s] MA: queue cleared in Music Assistant", self.name)
            self._report_interrupted()
            return
        if (
            cur_id == self._left_item_id
            and time.monotonic() - self._promoted_at < PROMOTE_SETTLE_SECONDS
            and (
                (queue.index_in_buffer or 0) > (queue.current_index or 0)
                or queue.corrected_elapsed_time >= 5
            )
        ):
            # "next" in MA: until the speaker starts the next track MA shows the one we
            # left (while already buffering a later one, or still at its old position;
            # "previous" in MA restarts it from 0)
            self._schedule_reevaluate(1.0)
            return
        if cur_id in self._own_item_ids and queue.active:
            # One of our earlier tracks, e.g. "previous" pressed in Music Assistant
            logger.info(
                "[%s] MA: went back to an earlier track in Music Assistant; "
                "showing stopped in the Qobuz app",
                self.name,
            )
            self._report_interrupted()
            return
        name = cur_item.name or "another item"
        if queue.state not in _PLAYING_OR_PAUSED:
            # Idle on another item (e.g. MA restarted and restored an older queue):
            # nothing else is playing, so this is a stop, not a takeover
            logger.info("[%s] MA: stopped; Music Assistant now shows %s", self.name, name)
            self._report_interrupted()
            return
        self._takeover(f"Music Assistant is now playing something else ({name})")

    def _evaluate_current(self, queue: Any, in_grace: bool) -> None:
        """MA's current item is our current track."""
        if not queue.active:
            if in_grace:
                self._schedule_reevaluate(self._grace_left() + 0.2)
            elif self._attached:
                self._takeover("the player switched to another source in Music Assistant")
            else:
                self._forget_current()
            return
        # Jumps only count when MA told us about them (polls only catch up)
        self._update_position(queue, report_jumps=self._in_ma_event)
        state = queue.state
        if not self._attached:
            self._ma_state = state
            return
        expected = self._pending_expectation()
        if expected is not None:
            if state == expected or (
                expected == MAPlaybackState.PAUSED and state == MAPlaybackState.IDLE
            ):
                self._expected_state = None  # MA confirmed our command
            else:
                # Still the state from before our command: wait for MA to catch up
                self._schedule_reevaluate(self._expected_until - time.monotonic() + 0.1)
                return
        if state == MAPlaybackState.PAUSED:
            if not self._paused_hold:
                logger.info("[%s] MA: paused in Music Assistant", self.name)
            self._paused_hold = True
            self._cancel_timer("idle")
        elif state == MAPlaybackState.PLAYING:
            if self._paused_hold:
                logger.info("[%s] MA: playing again in Music Assistant", self.name)
            self._paused_hold = False
            self._cancel_timer("idle")
            # Playing again after an MA action (e.g. a seek): a later idle is not its doing
            self._last_play_action_at = 0.0
            self._check_stall(queue)
        elif state == MAPlaybackState.IDLE:
            if in_grace:
                # A speaker that is still starting (or restarting the stream)
                self._schedule_reevaluate(self._grace_left() + 0.2)
                return
            if (
                not self._paused_hold
                and self._ma_state == MAPlaybackState.PLAYING
                and "idle" not in self._timers
            ):
                self._set_timer("idle", IDLE_SETTLE_SECONDS, self._on_idle_settled)
                return  # keep _ma_state PLAYING until the idle is confirmed
        self._ma_state = state

    def _on_idle_settled(self) -> None:
        """MA stayed idle on our track: it ended, or someone stopped it."""
        if self._busy or self._external or not self._attached or self._paused_hold:
            return
        queue = self._our_queue()
        if queue is None:
            self._evaluate()
            return
        if queue.state != MAPlaybackState.IDLE:
            return
        recent_action = time.monotonic() - self._last_play_action_at < (
            PLAY_ACTION_WINDOW_SECONDS + IDLE_SETTLE_SECONDS
        )
        if not self._seen_playing:
            logger.warning(
                "[%s] The speaker did not start playing track %s; showing stopped",
                self.name,
                self._current.track_id if self._current is not None else "?",
            )
            self._report_interrupted()
        elif (queue.ended or self._near_end()) and not recent_action:
            self._report_ended()
        else:
            logger.info(
                "[%s] MA: stopped in Music Assistant at %s",
                self.name,
                _format_ms(self._furthest_position_ms),
            )
            self._report_interrupted()

    def _check_stall(self, queue: Any) -> None:
        """MA says playing far past the end: it lost track of the speaker."""
        duration = self._current.duration_ms if self._current is not None else 0
        if duration <= 0 or self._in_grace():
            self._stall_since = None
            return
        if int(queue.corrected_elapsed_time * 1000) <= duration + STALL_MARGIN_MS:
            self._stall_since = None
            return
        now = time.monotonic()
        if self._stall_since is None:
            self._stall_since = now
        if now - self._stall_since < STALL_CONFIRM_SECONDS:
            self._schedule_reevaluate(STALL_CONFIRM_SECONDS - (now - self._stall_since) + 0.1)
            return
        logger.warning(
            "[%s] Music Assistant stopped reporting progress for this track (another app may "
            "be playing on the speaker); showing stopped in the Qobuz app",
            self.name,
        )
        self._report_interrupted()

    def _promote_armed(self, armed: _OwnItem, queue: Any) -> None:
        """MA moved onto the next track we queued (gapless, or "next" in MA)."""
        self._left_item_id = self._current.item_id if self._current is not None else None
        self._promoted_at = time.monotonic()
        self._current = armed
        self._armed = None
        self._armed_restored = False
        self._position_ms = 0
        self._position_at = time.monotonic()
        self._furthest_position_ms = 0
        self._seen_playing = queue.state == MAPlaybackState.PLAYING
        self._stall_since = None
        self._end_reported = False
        self._paused_hold = queue.state == MAPlaybackState.PAUSED
        self._play_started_at = time.monotonic()
        self._ma_state = MAPlaybackState.PLAYING
        self._cancel_timer("idle")
        self._save_session()
        if not self._attached:
            return
        logger.info("[%s] MA: moved on to the next Qobuz track %s", self.name, armed.track_id)
        self._notify_next_track_started()
        self._notify_position_update(0)

    def _step_past_stale(self, item_id: str) -> None:
        """MA reached a next track the Qobuz app no longer wants (it was already buffered)."""
        if self._stale_task is not None and not self._stale_task.done():
            return
        self._stale_item_ids.discard(item_id)
        armed = self._armed
        qid = self._player_id

        async def step_past() -> None:
            self._busy += 1
            try:
                if armed is not None:
                    logger.info(
                        "[%s] MA reached an outdated next track; jumping to %s",
                        self.name,
                        armed.track_id,
                    )
                    await self._conn.call(
                        f"player_queues/play_index queue={qid} item={armed.item_id}",
                        lambda c: c.player_queues.play_index(qid, armed.item_id),
                        timeout=PLAY_TIMEOUT_SECONDS,
                    )
                else:
                    logger.info("[%s] MA reached an outdated next track; stopping it", self.name)
                    await self._conn.call(
                        f"player_queues/stop queue={qid}", lambda c: c.player_queues.stop(qid)
                    )
                    self._report_ended()
            except (NotConnectedError, MusicAssistantError) as e:
                logger.warning("[%s] Could not step past the outdated track: %s", self.name, e)
            finally:
                self._busy -= 1
                asyncio.get_running_loop().call_soon(self._evaluate)

        self._stale_task = self._spawn(step_past())

    def _report_ended(self) -> None:
        if self._end_reported:
            return
        self._end_reported = True
        self._attached = False
        self._ma_state = MAPlaybackState.IDLE
        self._cancel_timer("idle")
        logger.info("[%s] MA: track finished", self.name)
        self._notify_state_change(PlaybackState.STOPPED)
        self._notify_track_ended()

    def _report_interrupted(self) -> None:
        if self._end_reported:
            return
        self._end_reported = True
        self._attached = False
        self._ma_state = MAPlaybackState.IDLE
        self._cancel_timer("idle")
        self._notify_state_change(PlaybackState.STOPPED)
        self._notify_playback_interrupted(self._furthest_position_ms)

    def _takeover(self, reason: str) -> None:
        """Someone else uses the player now: let go, never fight back."""
        if self._external:
            return
        self._external = True
        self._attached = False
        self._armed = None
        self._cancel_timer("idle")
        self._cancel_timer("jump")
        logger.warning(
            "[%s] %s — handing the player back to Music Assistant and leaving the Qobuz "
            "session (no fight-back)",
            self.name,
            reason,
        )
        self._notify_state_change(PlaybackState.STOPPED)
        if self._on_external_playback_cb is not None:
            self._takeover_task = self._spawn(self._on_external_playback_cb())

    def _own_item(self, queue_item: Any, metadata: BackendTrackMetadata) -> _OwnItem:
        item = _OwnItem(
            item_id=str(queue_item.queue_item_id),
            track_id=metadata.track_id,
            duration_ms=(
                int(queue_item.duration * 1000) if queue_item.duration else metadata.duration_ms
            ),
            title=metadata.title,
        )
        self._own_item_ids.add(item.item_id)
        self._stale_item_ids.discard(item.item_id)
        return item

    def _set_current(self, item: _OwnItem, metadata: BackendTrackMetadata) -> None:
        self._current = item
        self._current_meta = metadata
        self._position_ms = 0
        self._position_at = time.monotonic()
        self._furthest_position_ms = 0
        self._seen_playing = False
        self._end_reported = False
        self._paused_hold = False
        self._external = False
        self._stall_since = None
        self._left_item_id = None
        self._play_started_at = time.monotonic()
        self._ma_state = MAPlaybackState.PLAYING
        self._expect(MAPlaybackState.PLAYING)
        self._save_session()

    def _forget_current(self) -> None:
        self._current = None
        self._current_meta = None
        self._armed = None
        self._attached = False
        self._save_session()

    async def _find_new_item(self, track_id: str, offset: int) -> Optional[Any]:
        """Find the queue item MA just created for ``track_id`` (play_media returns nothing)."""
        qid = self._player_id
        items = await self._conn.call(
            f"player_queues/items queue={qid} offset={offset}",
            lambda c: c.player_queues.get_queue_items(qid, limit=20, offset=max(0, offset)),
        )
        for item in items:
            if item.queue_item_id not in self._own_item_ids and _is_track(item, track_id):
                return item
        return None

    async def _item_in_queue(self, item_id: str) -> bool:
        qid = self._player_id
        queue = self._conn.queue(qid)
        offset = (queue.current_index or 0) if queue is not None else 0
        items = await self._conn.call(
            f"player_queues/items queue={qid} offset={offset}",
            lambda c: c.player_queues.get_queue_items(qid, limit=20, offset=offset),
        )
        return any(item.queue_item_id == item_id for item in items)

    def _our_queue(self) -> Optional[Any]:
        """MA's queue, but only while its current item is our current track."""
        if self._current is None:
            return None
        queue = self._conn.queue(self._player_id)
        if queue is None or queue.current_item is None:
            return None
        if queue.current_item.queue_item_id != self._current.item_id:
            return None
        return queue

    def _ma_plays_ours(self) -> bool:
        """Whether MA is playing or paused on our current (or queued next) item.

        MA describes the speaker from its own queue, so another app playing on
        the speaker directly (e.g. a WiiM's built-in Qobuz Connect) is not
        visible here; the stall check catches it later.
        """
        if self._current is None:
            return False
        queue = self._conn.queue(self._player_id)
        if queue is None or not queue.active or queue.current_item is None:
            return False
        ours = {self._current.item_id}
        if self._armed is not None:
            ours.add(self._armed.item_id)
        return queue.current_item.queue_item_id in ours and queue.state in _PLAYING_OR_PAUSED

    def _foreign_content_playing(self, queue: Any) -> bool:
        if not queue.active:
            return True
        cur = queue.current_item
        return (
            cur is not None
            and cur.queue_item_id not in self._own_item_ids
            and queue.state in _PLAYING_OR_PAUSED
        )

    def _update_position(self, queue: Any, report_jumps: bool = False) -> None:
        now = time.monotonic()
        position = int(queue.corrected_elapsed_time * 1000)
        if self._current is not None and self._current.duration_ms > 0:
            position = min(position, self._current.duration_ms)
        position = max(0, position)
        # Where the track should be by now if nobody moved it
        expected = self._position_ms
        if self._ma_state == MAPlaybackState.PLAYING and self._position_at:
            expected += int((now - self._position_at) * 1000)
        if position < self._position_ms - 2000:
            self._furthest_position_ms = position  # moved back (seek or restart)
        self._position_ms = position
        self._position_at = now
        if queue.state == MAPlaybackState.PLAYING:
            self._seen_playing = True
            self._furthest_position_ms = max(self._furthest_position_ms, position)
        if (
            report_jumps
            and abs(position - expected) > 2000
            and queue.state in _PLAYING_OR_PAUSED
            and self._attached
            and not self._busy
            and not self._in_grace()
            and now - self._last_seek_at >= 3.0
        ):
            # Seek in MA's UI, or the speaker jumped by itself
            logger.info("[%s] MA: seek to %s in Music Assistant", self.name, _format_ms(position))
            self._notify_position_update(position)

    def _near_end(self) -> bool:
        duration = self._current.duration_ms if self._current is not None else 0
        if duration <= 0 or self._furthest_position_ms <= 0:
            return True  # unknown: treat as an end so the queue never stalls
        return duration - self._furthest_position_ms <= END_MARGIN_MS

    def _expect(self, state: MAPlaybackState) -> None:
        """Our command should bring MA to ``state``; ignore MA's older reports until then."""
        self._expected_state = state
        self._expected_until = time.monotonic() + EXPECT_STATE_SECONDS

    def _pending_expectation(self) -> Optional[MAPlaybackState]:
        if self._expected_state is not None and time.monotonic() >= self._expected_until:
            self._expected_state = None
        return self._expected_state

    def _in_grace(self) -> bool:
        return self._grace_left() > 0

    def _grace_left(self) -> float:
        return START_GRACE_SECONDS - (time.monotonic() - self._play_started_at)

    def _schedule_reevaluate(self, delay: float) -> None:
        if "reevaluate" not in self._timers:
            self._set_timer("reevaluate", max(0.05, delay), self._evaluate)

    def _set_timer(self, name: str, delay: float, callback: Callable[[], None]) -> None:
        """Run ``callback`` after ``delay``, replacing a pending timer of the same name."""
        self._cancel_timer(name)

        def fire() -> None:
            del self._timers[name]
            callback()

        self._timers[name] = asyncio.get_running_loop().call_later(delay, fire)

    def _cancel_timer(self, name: str) -> None:
        timer = self._timers.pop(name, None)
        if timer is not None:
            timer.cancel()

    def _spawn(self, coro: Awaitable[Any]) -> "asyncio.Future[Any]":
        task = asyncio.ensure_future(coro)
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)
        return task

    def _save_session(self) -> None:
        """Remember our MA items, so an add-on restart knows MA's current item is ours."""
        if self._current is None:
            self._save_session_cb(None)
            return
        session: dict[str, Any] = {"current": asdict(self._current)}
        if self._armed is not None:
            session["armed"] = asdict(self._armed)
        self._save_session_cb(session)

    def _restore_session(self) -> None:
        session = self._saved_session
        if not session:
            return
        try:
            current = _OwnItem(**session["current"])
            armed = _OwnItem(**session["armed"]) if session.get("armed") else None
        except (KeyError, TypeError) as e:
            logger.warning("[%s] Ignoring a saved session from another version: %s", self.name, e)
            return
        self._current, self._armed = current, armed
        self._own_item_ids.add(current.item_id)
        if armed is not None:
            self._armed_restored = True
            self._own_item_ids.add(armed.item_id)
        # Detached until the Qobuz app plays again: we only follow MA meanwhile.
        self._attached = False
        logger.info(
            "[%s] Remembered the last Qobuz track %s from before the restart",
            self.name,
            self._current.track_id,
        )
