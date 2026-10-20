"""
Audio backend for one Music Assistant player or group.

The bridge asks MA to play ``qobuz://track/<id>`` on the player's MA queue
(queue id == player id) and MA streams it with its own Qobuz provider, so MA
shows the track, artwork, queue and progress. The streaming URL the QobuzPlayer
looks up is not used; that lookup still checks the track is available.

Ownership:

- The MA queue items this backend created are "ours" (kept across add-on
  restarts in the settings file).
- While *attached* (the Qobuz app plays through us), MA moving onto our queued
  next item is a gapless transition, MA finishing our track is a track end,
  and MA stopping it is an interruption (the app shows stopped). After a stop,
  an interruption or a restart the backend is detached: it follows MA and
  never makes the QobuzPlayer advance.
- Anything else on the queue while attached means someone took the player
  over in MA: the backend lets go and sends MA nothing more until the Qobuz
  app selects the speaker again (no fight-back).

MA pushes its state over the websocket, so get_state() and get_position()
read the client's cache and never call MA.
"""

import asyncio
import logging
import time
from dataclasses import asdict, dataclass
from typing import Any, Awaitable, Callable, Optional

from music_assistant_models.enums import EventType, PlayerType, QueueOption, RepeatMode
from music_assistant_models.enums import PlaybackState as MusicAssistantState
from music_assistant_models.errors import (
    InvalidDataError,
    InvalidProviderURI,
    MediaNotFoundError,
    MusicAssistantError,
    PlayerUnavailableError,
    UnplayableMediaError,
)
from music_assistant_models.player import Player
from music_assistant_models.player_queue import PlayerQueue
from music_assistant_models.queue_item import QueueItem

from qobuz_proxy.backends.base import AudioBackend, ExternalPlaybackAware, TrackUnavailableError
from qobuz_proxy.backends.types import BackendTrackMetadata, PlaybackState

from .connection import (
    ConnectionState,
    ConnectionStatus,
    MusicAssistantConnection,
    NotConnectedError,
)

logger = logging.getLogger(__name__)

# Ignore MA's transitional states this long after we started or moved a track.
START_GRACE_SECONDS = 4.0
# MA "idle" must last this long before it counts (streams restart briefly on seek).
IDLE_SETTLE_SECONDS = 3.0
# An MA stop/next/seek marks the queue busy; an idle this soon after one is not a track end.
PLAY_ACTION_WINDOW_SECONDS = 3.0
# A stop with more than this left is not the track ending by itself.
END_MARGIN_MS = 10_000
# MA still reports "playing" this long past the track's end: it lost track of the player.
STALL_MARGIN_MS = 10_000
# ...and keeps doing so this long (right after a seek MA briefly adds the old stream's time).
STALL_CONFIRM_SECONDS = 5.0
# After "next" in MA, MA shows the track we left again (at its old position) until the
# player starts the next one; for this long that is not a "previous".
PROMOTE_SETTLE_SECONDS = 10.0
# Tell the app the track stopped if MA stays unreachable this long while playing.
DISCONNECT_GRACE_SECONDS = 20.0
# Seeks this close to where MA already is are skipped (MA restarts the stream to seek).
SEEK_TOLERANCE_MS = 1_500
# For this long after our seek, the app asking for it again or MA's position jumping is
# an echo of that seek, not a new one.
SEEK_ECHO_SECONDS = 3.0
# Right after taking over what MA plays, a seek back by up to this much is a stale position.
ADOPT_STALE_SEEK_MS = 300_000
# Don't adopt MA's current track right after we stopped it ourselves.
ADOPT_BLOCK_AFTER_STOP_SECONDS = 5.0
# Wait this long after a track starts before queuing the next one in MA.
NEXT_ARM_DELAY_SECONDS = 1.5
# Wait before retrying to queue a next track after an MA error.
NEXT_RETRY_COOLDOWN_SECONDS = 5.0
# Don't try to queue a next track for this long when the user queued their own in MA.
EXTERNAL_NEXT_COOLDOWN_SECONDS = 30.0
# Keep the queued next track this long for play() after stop(next_track_id=...).
JUMP_WAIT_SECONDS = 3.0
# After our pause/resume/play, wait this long for MA to report it before trusting MA's state
# again (a real player can take seconds to confirm, and MA keeps reporting the old state).
EXPECT_STATE_SECONDS = 10.0
# Coalesce volume changes made in MA; ignore MA's echoes this long after we set one.
VOLUME_DEBOUNCE_SECONDS = 0.3
VOLUME_SETTLE_SECONDS = 1.5
# Starting at or below this position is a normal start, not a positioned one.
POSITIONED_START_MIN_MS = 2_000
# MA can take a while to resolve, buffer and start a track on a player or group.
PLAY_TIMEOUT_SECONDS = 30.0

_UNPLAYABLE_ERRORS = (MediaNotFoundError, UnplayableMediaError, InvalidProviderURI)
_PLAYING_OR_PAUSED = (MusicAssistantState.PLAYING, MusicAssistantState.PAUSED)


@dataclass
class _OurItem:
    """An MA queue item this backend created."""

    item_id: str  # MA queue_item_id
    track_id: str  # Qobuz track id
    duration_ms: int


def _format_ms(ms: int) -> str:
    seconds = max(0, ms) // 1000
    return f"{seconds // 60}:{seconds % 60:02d}"


def _qobuz_uri(track_id: str) -> str:
    return f"qobuz://track/{track_id}"


def _is_track(item: QueueItem, track_id: str) -> bool:
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


def _read_volume(player: Player) -> Optional[int]:
    """A group (or a player leading one) has a group volume; a single player its own."""
    grouped = bool(player.group_members) or player.type == PlayerType.GROUP
    value = player.group_volume if grouped and player.group_volume is not None else None
    if value is None:
        value = player.volume_level
    return int(value) if value is not None else None


class MusicAssistantBackend(AudioBackend, ExternalPlaybackAware):
    """Audio backend for one Music Assistant player or group."""

    def __init__(
        self,
        connection: MusicAssistantConnection,
        player_id: str,
        name: str,
        session: Optional[dict[str, Any]] = None,
        save_session: Callable[[Optional[dict[str, Any]]], None] = lambda session: None,
    ) -> None:
        super().__init__(name=name)
        self._connection = connection
        self._player_id = player_id
        self._saved_session = session
        self._save_session_callback = save_session

        self._current: Optional[_OurItem] = None
        self._current_meta: Optional[BackendTrackMetadata] = None
        self._armed: Optional[_OurItem] = None  # our next track, already queued in MA
        self._our_item_ids: set[str] = set()
        self._stale_item_ids: set[str] = set()  # queued next items the app no longer wants

        self._is_attached = False  # the Qobuz app is playing through us right now
        self._has_external_playback = False  # someone else took the player over in MA
        self._writes_in_flight = 0  # our own multi-step MA writes in flight
        self._is_arming = False
        self._play_started_at = 0.0
        self._last_play_action_at = 0.0
        self._last_stop_at: Optional[float] = None
        self._queue_state: Optional[MusicAssistantState] = None
        self._is_paused = False
        self._has_reported_end = False
        self._position_ms = 0
        self._position_at = 0.0  # time.monotonic() of the last position update
        self._furthest_position_ms = 0
        self._has_played = False  # MA reported our current item as playing
        self._is_handling_event = False  # handling an MA event (not a poll or a timer)
        self._is_armed_restored = False  # _armed came from the settings file
        self._last_seek_target: Optional[int] = None
        self._last_seek_at = 0.0
        self._adopt_seek_until = 0.0
        self._adopt_position_ms = 0
        self._is_queue_prepared = False
        self._expected_state: Optional[MusicAssistantState] = None
        self._expected_until = 0.0
        self._stall_since: Optional[float] = None
        self._left_item_id: Optional[str] = None  # current item before the last promotion
        self._promoted_at = 0.0

        self._failed_next: set[tuple[str, int]] = set()
        self._next_retry_after = 0.0

        self._pending_volume: Optional[int] = None
        self._volume_lock = asyncio.Lock()
        self._volume_settle_until = 0.0
        self._last_music_assistant_volume: Optional[int] = None

        self._timers: dict[str, asyncio.TimerHandle] = {}
        self._external_playback_callback: Optional[Callable[[], Awaitable[None]]] = None
        self._let_go_task: Optional["asyncio.Future[Any]"] = None
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

        Succeeds while MA is unreachable too: the Qobuz speaker stays visible
        and commands fail with a clear error until the connection is back.
        """
        if self._is_connected:
            return True
        self._restore_session()
        self._connection.add_event_listener(self._player_id, self._on_event)
        self._connection.add_status_listener(self._on_connection_status)
        self._is_connected = True
        self._sync_volume_from_music_assistant()
        logger.info("[%s] Music Assistant backend ready (player %s)", self.name, self._player_id)
        return True

    async def disconnect(self) -> None:
        """Stop listening. MA keeps playing: an add-on restart must not stop the music."""
        if not self._is_connected:
            return
        self._is_connected = False
        self._connection.remove_event_listener(self._player_id, self._on_event)
        self._connection.remove_status_listener(self._on_connection_status)
        for timer in self._timers.values():
            timer.cancel()
        self._timers.clear()
        for task in list(self._tasks):
            task.cancel()

    async def play(self, url: str, metadata: BackendTrackMetadata) -> None:
        """Play a Qobuz track through Music Assistant (``url`` is not used)."""
        start_ms = metadata.start_position_ms
        logger.info(
            "[%s] Qobuz app: play track %s '%s' – %s%s",
            self.name,
            metadata.track_id,
            metadata.title,
            metadata.artist,
            f" from {_format_ms(start_ms)}" if start_ms >= POSITIONED_START_MIN_MS else "",
        )
        if not self._connection.is_connected:
            raise NotConnectedError("Music Assistant is not connected")
        if self._has_external_playback:
            raise RuntimeError(
                "Another source is playing on this player in Music Assistant; "
                "select it again in the Qobuz app"
            )
        self._cancel_timer("jump")
        self._cancel_timer("idle")
        adopted = False
        self._writes_in_flight += 1
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
            self._is_attached = True
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
            self._writes_in_flight -= 1
        # The QobuzPlayer records PLAYING and the start position once we return; look at
        # MA's state (and report MA's real position after an adoption) after that.
        loop = asyncio.get_running_loop()
        if adopted:
            loop.call_soon(self._notify_position_update, self._position_ms)
        loop.call_soon(self._check_queue)

    async def _adopt(self, metadata: BackendTrackMetadata) -> Optional[_OurItem]:
        """Continue MA's current item if it already plays this track.

        After an add-on restart (or a new selection) the Qobuz app asks for the
        track MA is still playing; restarting it would be audible.
        """
        if (
            self._last_stop_at is not None
            and time.monotonic() - self._last_stop_at < ADOPT_BLOCK_AFTER_STOP_SECONDS
        ):
            return None
        queue = self._connection.queue(self._player_id)
        if queue is None or not queue.active or queue.current_item is None:
            return None
        if queue.state not in _PLAYING_OR_PAUSED:
            return None
        current = queue.current_item
        if not _is_track(current, metadata.track_id):
            return None
        if self._current is not None and current.queue_item_id != self._current.item_id:
            return None
        logger.info(
            "[%s] Music Assistant is already playing track %s; continuing it without restarting",
            self.name,
            metadata.track_id,
        )
        if queue.state == MusicAssistantState.PAUSED:
            queue_id = self._player_id
            await self._connection.call(
                f"player_queues/play queue={queue_id} (resume the track MA has)",
                lambda c: c.player_queues.play(queue_id),
            )
        self._adopt_position_ms = int(queue.corrected_elapsed_time * 1000)
        self._adopt_seek_until = time.monotonic() + START_GRACE_SECONDS
        return self._claim(current, metadata)

    async def _jump_to_armed(
        self, metadata: BackendTrackMetadata, start_ms: int
    ) -> Optional[_OurItem]:
        """Play the next track MA already has queued (a "next" in the Qobuz app)."""
        armed = self._armed
        if armed is None or armed.track_id != metadata.track_id:
            return None
        queue_id = self._player_id
        seek_s = start_ms // 1000 if start_ms >= POSITIONED_START_MIN_MS else 0
        try:
            await self._connection.call(
                f"player_queues/play_index queue={queue_id} item={armed.item_id}",
                lambda c: c.player_queues.play_index(queue_id, armed.item_id, seek_position=seek_s),
                timeout=PLAY_TIMEOUT_SECONDS,
            )
        except InvalidDataError:
            self._armed = None  # the queued item is gone (queue edited in MA): start afresh
            return None
        self._armed = None
        return armed

    async def _start_track(self, metadata: BackendTrackMetadata, start_ms: int) -> _OurItem:
        """Replace the MA queue with this track, optionally starting mid-track."""
        uri = _qobuz_uri(metadata.track_id)
        queue_id = self._player_id
        if self._armed is not None:
            self._stale_item_ids.add(self._armed.item_id)
            self._armed = None
        found: Optional[QueueItem] = None
        if start_ms < POSITIONED_START_MIN_MS:
            await self._connection.call(
                f"player_queues/play_media queue={queue_id} media={uri} option=replace",
                lambda c: c.player_queues.play_media(
                    queue_id, media=uri, option=QueueOption.REPLACE
                ),
                timeout=PLAY_TIMEOUT_SECONDS,
            )
            queue = self._connection.queue(queue_id)
            current = queue.current_item if queue is not None else None
            if (
                current is not None
                and current.queue_item_id not in self._our_item_ids
                and _is_track(current, metadata.track_id)
            ):
                found = current
            else:
                found = await self._find_new_item(metadata.track_id, offset=0)
        else:
            # One stream start at the right spot instead of play-then-seek
            await self._connection.call(
                f"player_queues/clear queue={queue_id}", lambda c: c.player_queues.clear(queue_id)
            )
            await self._connection.call(
                f"player_queues/play_media queue={queue_id} media={uri} option=add",
                lambda c: c.player_queues.play_media(queue_id, media=uri, option=QueueOption.ADD),
                timeout=PLAY_TIMEOUT_SECONDS,
            )
            found = await self._find_new_item(metadata.track_id, offset=0)
            if found is not None:
                seek_s = start_ms // 1000
                item_id = found.queue_item_id
                await self._connection.call(
                    f"player_queues/play_index queue={queue_id} item={item_id} seek={seek_s}s",
                    lambda c: c.player_queues.play_index(queue_id, item_id, seek_position=seek_s),
                    timeout=PLAY_TIMEOUT_SECONDS,
                )
        if found is None:
            raise MediaNotFoundError(
                f"Music Assistant did not queue Qobuz track {metadata.track_id}"
            )
        # Only after the queue was replaced: a dynamic queue refuses repeat changes before
        await self._prepare_queue()
        return self._claim(found, metadata)

    async def _prepare_queue(self) -> None:
        """Turn off MA queue settings that would fight the Qobuz queue (once per selection)."""
        if self._is_queue_prepared:
            return
        queue = self._connection.queue(self._player_id)
        if queue is None:
            return
        self._is_queue_prepared = True
        queue_id = self._player_id
        try:
            if queue.repeat_mode != RepeatMode.OFF:
                await self._connection.call(
                    f"player_queues/repeat queue={queue_id} repeat_mode=off",
                    lambda c: c.player_queues.repeat(queue_id, RepeatMode.OFF),
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
                await self._connection.call(
                    f"player_queues/autoplay queue={queue_id} autoplay_enabled=false",
                    lambda c: c.send_command(
                        "player_queues/autoplay", queue_id=queue_id, autoplay_enabled=False
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
        logger.info("[%s] Qobuz app: pause", self.name)
        if not self._connection.is_connected:
            raise NotConnectedError("Music Assistant is not connected")
        self._cancel_timer("idle")
        if self._queue_plays_ours():
            queue_id = self._player_id
            await self._connection.call(
                f"player_queues/pause queue={queue_id}", lambda c: c.player_queues.pause(queue_id)
            )
            self._expect(MusicAssistantState.PAUSED)
        self._is_paused = True
        self._notify_state_change(PlaybackState.PAUSED)

    async def resume(self) -> bool:
        logger.info("[%s] Qobuz app: resume", self.name)
        if (
            not self._connection.is_connected
            or self._has_external_playback
            or self._current is None
        ):
            return False
        current = self._current
        queue_id = self._player_id
        queue = self._connection.queue(queue_id)
        if (
            queue is not None
            and queue.active
            and queue.current_item is not None
            and queue.current_item.queue_item_id == current.item_id
        ):
            # MA's queue state lags the player by ~0.5 s: right after our own pause
            # (e.g. the re-pause after a seek) the queue still says playing
            player = self._connection.player(queue_id)
            player_paused = (
                player is not None and player.playback_state == MusicAssistantState.PAUSED
            )
            if queue.state == MusicAssistantState.PLAYING and not player_paused:
                self._resumed()
                return True
            if queue.state in _PLAYING_OR_PAUSED or (
                queue.state == MusicAssistantState.IDLE and not queue.ended
            ):
                self._writes_in_flight += 1
                try:
                    await self._connection.call(
                        f"player_queues/play queue={queue_id}",
                        lambda c: c.player_queues.play(queue_id),
                    )
                except (NotConnectedError, MusicAssistantError):
                    return False
                finally:
                    self._writes_in_flight -= 1
                self._resumed()
                return True
        if queue is not None and self._external_content_playing(queue):
            self._let_go("Music Assistant is playing something else")
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
        self._is_paused = False
        self._is_attached = True
        self._queue_state = MusicAssistantState.PLAYING
        self._play_started_at = time.monotonic()
        self._expect(MusicAssistantState.PLAYING)
        self._notify_state_change(PlaybackState.PLAYING)
        asyncio.get_running_loop().call_soon(self._check_queue)

    async def stop(self, *, next_track_id: Optional[str] = None) -> None:
        self._cancel_timer("idle")
        armed = self._armed
        self._is_attached = False
        self._is_paused = False
        if next_track_id is not None and armed is not None and armed.track_id == next_track_id:
            # play() is about to jump straight to the item MA already has queued.
            # If it doesn't come (load superseded, skip while paused), stop then.
            logger.info("[%s] Qobuz app: stop (switching to the queued next track)", self.name)
            self._set_timer("jump", JUMP_WAIT_SECONDS, self._on_jump_timeout)
            self._notify_state_change(PlaybackState.STOPPED)
            return
        logger.info("[%s] Qobuz app: stop", self.name)
        self._cancel_timer("jump")
        await self._stop_music_assistant()
        if armed is not None:
            self._stale_item_ids.add(armed.item_id)
        self._armed = None
        self._notify_state_change(PlaybackState.STOPPED)

    async def _stop_music_assistant(self) -> None:
        """Stop MA, but only while it still plays our track."""
        if not self._queue_plays_ours():
            return
        queue_id = self._player_id
        self._writes_in_flight += 1
        try:
            await self._connection.call(
                f"player_queues/stop queue={queue_id}", lambda c: c.player_queues.stop(queue_id)
            )
            self._last_stop_at = time.monotonic()
            self._queue_state = MusicAssistantState.IDLE
        except (NotConnectedError, MusicAssistantError) as e:
            logger.warning("[%s] Music Assistant did not stop: %s", self.name, e)
        finally:
            self._writes_in_flight -= 1

    def _on_jump_timeout(self) -> None:
        if self._is_attached:
            return
        if self._armed is not None:
            self._stale_item_ids.add(self._armed.item_id)
            self._armed = None
        logger.info("[%s] The queued next track was not played; stopping", self.name)
        self._run_in_background(self._stop_music_assistant())

    async def seek(self, position_ms: int) -> None:
        now = time.monotonic()
        live = self._live_position_ms()
        if now < self._adopt_seek_until and live - ADOPT_STALE_SEEK_MS <= position_ms <= (
            live + SEEK_TOLERANCE_MS
        ):
            logger.info(
                "[%s] Qobuz app: seek to %s skipped: Music Assistant kept playing at %s",
                self.name,
                _format_ms(position_ms),
                _format_ms(live),
            )
            return
        if (
            self._last_seek_target is not None
            and now - self._last_seek_at < SEEK_ECHO_SECONDS
            and abs(position_ms - self._last_seek_target) <= SEEK_TOLERANCE_MS
        ) or (self._our_queue() is not None and abs(position_ms - live) <= SEEK_TOLERANCE_MS):
            logger.debug("[%s] Already at %s; skipping seek", self.name, _format_ms(position_ms))
            return
        logger.info("[%s] Qobuz app: seek to %s", self.name, _format_ms(position_ms))
        if not self._connection.is_connected:
            raise NotConnectedError("Music Assistant is not connected")
        if not self._queue_plays_ours():
            raise RuntimeError("Cannot seek: Music Assistant is not playing this Qobuz track")
        queue_id = self._player_id
        seconds = int(round(position_ms / 1000))
        was_paused = self._is_paused
        self._cancel_timer("idle")
        self._writes_in_flight += 1
        try:
            await self._connection.call(
                f"player_queues/seek queue={queue_id} position={seconds}s",
                lambda c: c.player_queues.seek(queue_id, seconds),
                timeout=PLAY_TIMEOUT_SECONDS,
            )
            if was_paused:
                # MA starts playing when a paused queue seeks; keep it paused
                await self._connection.call(
                    f"player_queues/pause queue={queue_id} (stay paused after seek)",
                    lambda c: c.player_queues.pause(queue_id),
                )
        finally:
            self._writes_in_flight -= 1
        self._expect(MusicAssistantState.PAUSED if was_paused else MusicAssistantState.PLAYING)
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
        if self._has_external_playback:
            logger.info(
                "[%s] Qobuz app: volume %s%% ignored (not our player now)", self.name, level
            )
            return
        self._volume = level
        self._pending_volume = level
        self._volume_settle_until = time.monotonic() + VOLUME_SETTLE_SECONDS
        async with self._volume_lock:
            # A newer value queued behind us wins; only send the latest one.
            if self._pending_volume != level:
                return
            self._pending_volume = None
            if not self._connection.is_connected:
                logger.warning(
                    "[%s] Qobuz app: volume %s%% not applied: Music Assistant is not connected",
                    self.name,
                    level,
                )
                return
            logger.info("[%s] Qobuz app: volume %s%%", self.name, level)
            self._last_music_assistant_volume = level
            player_id = self._player_id
            # group_volume sets a group (or a player leading one) as a whole and
            # falls back to a plain volume set for a single player.
            await self._connection.call(
                f"players/cmd/group_volume player={player_id} level={level}",
                lambda c: c.players.group_volume(player_id, level),
            )
            self._volume_settle_until = time.monotonic() + VOLUME_SETTLE_SECONDS

    async def get_volume(self) -> int:
        if time.monotonic() < self._volume_settle_until:
            return self._volume  # MA has not caught up with our last change yet
        player = self._connection.player(self._player_id)
        level = _read_volume(player) if player is not None else None
        if level is not None:
            self._volume = level
        return self._volume

    def _sync_volume_from_music_assistant(self) -> None:
        """Start from MA's level, so a later change in MA is not taken for our own echo."""
        player = self._connection.player(self._player_id)
        level = _read_volume(player) if player is not None else None
        if level is not None:
            self._volume = self._last_music_assistant_volume = level

    async def get_state(self) -> PlaybackState:
        # The QobuzPlayer polls this every 0.5 s while playing or paused, which also
        # catches decisions a quiet MA (no further events) would leave pending.
        self._check_queue()
        if self._writes_in_flight:
            return PlaybackState.PLAYING if self._state == PlaybackState.STOPPED else self._state
        if self._current is None or not self._is_attached:
            return PlaybackState.STOPPED
        if not self._connection.is_connected:
            return self._state  # frozen until MA is back
        queue = self._our_queue()
        if queue is None:
            return self._state
        expected = self._pending_expectation()
        if expected is not None and queue.state != expected:
            # MA has not caught up with our last command yet
            return (
                PlaybackState.PAUSED
                if expected == MusicAssistantState.PAUSED
                else PlaybackState.PLAYING
            )
        if queue.state == MusicAssistantState.PLAYING:
            return PlaybackState.PLAYING
        if queue.state == MusicAssistantState.PAUSED or self._is_paused:
            return PlaybackState.PAUSED
        # Idle but not decided yet (settling, or a player still starting)
        if "idle" in self._timers or self._is_in_grace():
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
            self._is_armed_restored = False
            return True
        now = time.monotonic()
        if key in self._failed_next or now < self._next_retry_after:
            return False
        if (
            self._is_arming
            or self._writes_in_flight
            or not self._is_attached
            or not self._queue_plays_ours()
        ):
            return False
        queue_id = self._player_id
        queue = self._connection.queue(queue_id)
        if queue is None or queue.state != MusicAssistantState.PLAYING:
            return False  # MA only hands the next track to a playing player
        if queue.extra_attributes.get("play_action_in_progress") or (
            now - self._play_started_at < NEXT_ARM_DELAY_SECONDS
        ):
            return False
        current_index = queue.current_index or 0
        uri = _qobuz_uri(metadata.track_id)
        self._is_arming = True
        try:
            after = await self._connection.call(
                f"player_queues/items queue={queue_id} offset={current_index + 1}",
                lambda c: c.player_queues.get_queue_items(
                    queue_id, limit=10, offset=current_index + 1
                ),
            )
            first = after[0] if after else None
            if (
                first is not None
                and _is_track(first, metadata.track_id)
                and (
                    first.queue_item_id in self._our_item_ids
                    or queue.index_in_buffer == current_index + 1
                )
            ):
                # MA already has this track next (e.g. queued before an add-on restart)
                self._arm(self._claim(first, metadata))
                return True
            if any(i.queue_item_id not in self._our_item_ids for i in after):
                logger.info(
                    "[%s] Music Assistant has other tracks queued after this one; leaving "
                    "them alone (Music Assistant takes over when this track ends)",
                    self.name,
                )
                self._next_retry_after = now + EXTERNAL_NEXT_COOLDOWN_SECONDS
                return False
            logger.info(
                "[%s] Qobuz app: queue next track %s '%s'",
                self.name,
                metadata.track_id,
                metadata.title,
            )
            if armed is not None:
                # REPLACE_NEXT drops it unless the player already buffered it
                self._stale_item_ids.add(armed.item_id)
                self._armed = None
            await self._connection.call(
                f"player_queues/play_media queue={queue_id} media={uri} option=replace_next",
                lambda c: c.player_queues.play_media(
                    queue_id, media=uri, option=QueueOption.REPLACE_NEXT
                ),
                timeout=PLAY_TIMEOUT_SECONDS,
            )
            found = await self._find_new_item(metadata.track_id, offset=current_index)
            if found is None:
                raise MediaNotFoundError(f"Music Assistant did not queue track {metadata.track_id}")
            self._arm(self._claim(found, metadata))
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
            self._is_arming = False
            # Runs after the QobuzPlayer recorded the queued track
            asyncio.get_running_loop().call_soon(self._check_queue)

    def _arm(self, item: _OurItem) -> None:
        self._armed = item
        self._is_armed_restored = False
        self._save_session()

    async def clear_next_track(self) -> None:
        armed = self._armed
        if armed is None:
            return
        if self._is_armed_restored:
            # The QobuzPlayer's first clear after an add-on restart only resets its own (empty)
            # gapless state. Keep MA's item: set_next_track() takes it over if it still fits,
            # and REPLACE_NEXT replaces it if not.
            self._is_armed_restored = False
            return
        self._armed = None
        # If MA reaches it anyway (already buffered), we step past it
        self._stale_item_ids.add(armed.item_id)
        self._save_session()
        if not self._connection.is_connected or self._has_external_playback:
            return
        queue_id = self._player_id
        logger.info(
            "[%s] Qobuz app: next track changed; removing %s from Music Assistant",
            self.name,
            armed.track_id,
        )
        self._writes_in_flight += 1
        try:
            await self._connection.call(
                f"player_queues/delete_item queue={queue_id} item={armed.item_id}",
                lambda c: c.player_queues.delete_item(queue_id, armed.item_id),
            )
            if not await self._item_in_queue(armed.item_id):
                self._stale_item_ids.discard(armed.item_id)
        except (NotConnectedError, MusicAssistantError) as e:
            logger.debug("[%s] Could not remove the queued next track: %s", self.name, e)
        finally:
            self._writes_in_flight -= 1
            asyncio.get_running_loop().call_soon(self._check_queue)

    def on_external_playback(self, callback: Callable[[], Awaitable[None]]) -> None:
        self._external_playback_callback = callback

    def prepare_for_selection(self) -> None:
        """A new selection in the Qobuz app may take the player back."""
        self._is_queue_prepared = False
        if self._has_external_playback:
            self._has_external_playback = False
            self._current = None
            self._current_meta = None
            self._armed = None

    async def check_external_playback(self) -> bool:
        self._check_queue()
        task = self._let_go_task
        if task is not None and not task.done():
            # Release the old session before a new selection builds on it
            await asyncio.gather(task, return_exceptions=True)
        return self._has_external_playback

    async def can_apply_remote_state(self) -> bool:
        """Refuse replayed Qobuz snapshots while MA plays something else."""
        if self._has_external_playback:
            return False
        if self._current is None or not self._connection.is_connected:
            return True
        if self._is_in_grace() or self._queue_plays_ours():
            return True
        queue = self._connection.queue(self._player_id)
        return queue is None or not self._external_content_playing(queue)

    def _on_event(self, event: Any) -> None:
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
            if self._connection.queue(self._player_id) is not None:
                self._cancel_timer("disconnect")
        self._is_handling_event = True
        try:
            self._check_queue()
        finally:
            self._is_handling_event = False

    def _on_connection_status(self, status: ConnectionStatus) -> None:
        if status.state != ConnectionState.CONNECTED:
            self._start_disconnect_timer()
            return
        self._sync_volume_from_music_assistant()
        # MA may not have registered the player again yet after a restart
        if self._connection.queue(self._player_id) is not None:
            self._cancel_timer("disconnect")
        self._check_queue()

    def _start_disconnect_timer(self) -> None:
        if (
            self._current is None
            or not self._is_attached
            or self._state != PlaybackState.PLAYING
            or "disconnect" in self._timers
        ):
            return
        self._set_timer("disconnect", DISCONNECT_GRACE_SECONDS, self._on_disconnect_grace_expired)

    def _on_disconnect_grace_expired(self) -> None:
        if not self._is_attached or self._current is None or self._has_external_playback:
            return
        if self._connection.is_connected and self._connection.queue(self._player_id) is not None:
            self._check_queue()
            return
        logger.warning(
            "[%s] Lost Music Assistant; showing the track as stopped in the Qobuz app", self.name
        )
        self._report_interrupted()

    def _check_device_volume(self) -> None:
        player = self._connection.player(self._player_id)
        if player is None:
            return
        level = _read_volume(player)
        if level is None or level == self._last_music_assistant_volume:
            return
        self._last_music_assistant_volume = level
        if time.monotonic() < self._volume_settle_until or abs(level - self._volume) <= 1:
            return  # our own change echoed back
        self._volume = level
        self._set_timer("volume", VOLUME_DEBOUNCE_SECONDS, self._report_device_volume)

    def _report_device_volume(self) -> None:
        if self._has_external_playback:
            return
        logger.info("[%s] Music Assistant: volume changed to %s%%", self.name, self._volume)
        self._notify_volume_change(self._volume)

    def _check_queue(self) -> None:
        """Map MA's queue state onto the QobuzPlayer callbacks (sync; never awaits)."""
        if (
            self._writes_in_flight
            or self._current is None
            or self._has_external_playback
            or not self._is_connected
            or not self._connection.is_connected
        ):
            return
        queue = self._connection.queue(self._player_id)
        if queue is None:
            return
        if queue.extra_attributes.get("play_action_in_progress"):
            # MA is switching tracks or handling a stop/next/seek: wait for the
            # settled state, and remember it so a following idle isn't a track end.
            self._last_play_action_at = time.monotonic()
            self._check_queue_later(1.0)
            return
        current_item = queue.current_item
        current_id = current_item.queue_item_id if current_item is not None else None
        in_grace = self._is_in_grace()

        if current_id == self._current.item_id:
            self._check_our_item(queue, in_grace)
            return

        armed = self._armed
        if armed is not None and current_id == armed.item_id:
            self._promote_armed(armed, queue)
            return

        if current_id is not None and current_id in self._stale_item_ids:
            if self._is_attached:
                self._step_past_stale(current_id)
            return

        if in_grace or self._is_arming:
            self._check_queue_later(self._grace_left() + 0.2)
            return

        if not self._is_attached:
            # Nothing of ours is live in the Qobuz app: just follow MA
            if current_id is None or current_id not in self._our_item_ids or not queue.active:
                logger.debug("[%s] Music Assistant moved on to something else", self.name)
                self._forget_current()
            return

        if current_item is None:
            logger.info("[%s] Music Assistant: queue cleared", self.name)
            self._report_interrupted()
            return
        if (
            current_id == self._left_item_id
            and time.monotonic() - self._promoted_at < PROMOTE_SETTLE_SECONDS
            and (
                (queue.index_in_buffer or 0) > (queue.current_index or 0)
                or queue.corrected_elapsed_time >= 5
            )
        ):
            # "next" in MA: until the player starts the next track MA shows the one we
            # left (while already buffering a later one, or still at its old position;
            # "previous" in MA restarts it from 0)
            self._check_queue_later(1.0)
            return
        if current_id in self._our_item_ids and queue.active:
            # One of our earlier tracks, e.g. "previous" pressed in Music Assistant
            logger.info(
                "[%s] Music Assistant: went back to an earlier track; "
                "showing stopped in the Qobuz app",
                self.name,
            )
            self._report_interrupted()
            return
        name = current_item.name or "another item"
        if queue.state not in _PLAYING_OR_PAUSED:
            # Idle on another item (e.g. MA restarted and restored an older queue):
            # nothing else is playing, so this is a stop, not external playback
            logger.info("[%s] Music Assistant: stopped, now shows %s", self.name, name)
            self._report_interrupted()
            return
        self._let_go(f"Music Assistant is now playing something else ({name})")

    def _check_our_item(self, queue: PlayerQueue, in_grace: bool) -> None:
        """MA's current item is our current track."""
        if not queue.active:
            if in_grace:
                self._check_queue_later(self._grace_left() + 0.2)
            elif self._is_attached:
                self._let_go("the player switched to another source in Music Assistant")
            else:
                self._forget_current()
            return
        # Jumps only count when MA told us about them (polls only catch up)
        self._update_position(queue, report_jumps=self._is_handling_event)
        state = queue.state
        if not self._is_attached:
            self._queue_state = state
            return
        expected = self._pending_expectation()
        if expected is not None:
            if state == expected or (
                expected == MusicAssistantState.PAUSED and state == MusicAssistantState.IDLE
            ):
                self._expected_state = None  # MA confirmed our command
            else:
                # Still the state from before our command: wait for MA to catch up
                self._check_queue_later(self._expected_until - time.monotonic() + 0.1)
                return
        if state == MusicAssistantState.PAUSED:
            if not self._is_paused:
                logger.info("[%s] Music Assistant: paused", self.name)
            self._is_paused = True
            self._cancel_timer("idle")
        elif state == MusicAssistantState.PLAYING:
            if self._is_paused:
                logger.info("[%s] Music Assistant: playing again", self.name)
            self._is_paused = False
            self._cancel_timer("idle")
            # Playing again after an MA action (e.g. a seek): a later idle is not its doing
            self._last_play_action_at = 0.0
            self._check_stall(queue)
        elif state == MusicAssistantState.IDLE:
            if in_grace:
                # A player that is still starting (or restarting the stream)
                self._check_queue_later(self._grace_left() + 0.2)
                return
            if (
                not self._is_paused
                and self._queue_state == MusicAssistantState.PLAYING
                and "idle" not in self._timers
            ):
                self._set_timer("idle", IDLE_SETTLE_SECONDS, self._on_idle_settled)
                return  # keep _queue_state PLAYING until the idle is confirmed
        self._queue_state = state

    def _on_idle_settled(self) -> None:
        """MA stayed idle on our track: it ended, or someone stopped it."""
        if (
            self._writes_in_flight
            or self._has_external_playback
            or not self._is_attached
            or self._is_paused
        ):
            return
        queue = self._our_queue()
        if queue is None:
            self._check_queue()
            return
        if queue.state != MusicAssistantState.IDLE:
            return
        recent_action = time.monotonic() - self._last_play_action_at < (
            PLAY_ACTION_WINDOW_SECONDS + IDLE_SETTLE_SECONDS
        )
        if not self._has_played:
            logger.warning(
                "[%s] The player did not start playing track %s; showing stopped",
                self.name,
                self._current.track_id if self._current is not None else "?",
            )
            self._report_interrupted()
        elif (queue.ended or self._is_near_end()) and not recent_action:
            self._report_ended()
        else:
            logger.info(
                "[%s] Music Assistant: stopped at %s",
                self.name,
                _format_ms(self._furthest_position_ms),
            )
            self._report_interrupted()

    def _check_stall(self, queue: PlayerQueue) -> None:
        """MA says playing far past the end: it lost track of the player."""
        duration = self._current.duration_ms if self._current is not None else 0
        if duration <= 0 or self._is_in_grace():
            self._stall_since = None
            return
        if int(queue.corrected_elapsed_time * 1000) <= duration + STALL_MARGIN_MS:
            self._stall_since = None
            return
        now = time.monotonic()
        if self._stall_since is None:
            self._stall_since = now
        if now - self._stall_since < STALL_CONFIRM_SECONDS:
            self._check_queue_later(STALL_CONFIRM_SECONDS - (now - self._stall_since) + 0.1)
            return
        logger.warning(
            "[%s] Music Assistant stopped reporting progress for this track (another app may "
            "be playing on the player); showing stopped in the Qobuz app",
            self.name,
        )
        self._report_interrupted()

    def _promote_armed(self, armed: _OurItem, queue: PlayerQueue) -> None:
        """MA moved onto the next track we queued (gapless, or "next" in MA)."""
        self._left_item_id = self._current.item_id if self._current is not None else None
        self._promoted_at = time.monotonic()
        self._current = armed
        self._armed = None
        self._is_armed_restored = False
        self._position_ms = 0
        self._position_at = time.monotonic()
        self._furthest_position_ms = 0
        self._has_played = queue.state == MusicAssistantState.PLAYING
        self._stall_since = None
        self._has_reported_end = False
        self._is_paused = queue.state == MusicAssistantState.PAUSED
        self._play_started_at = time.monotonic()
        self._queue_state = MusicAssistantState.PLAYING
        self._cancel_timer("idle")
        self._save_session()
        if not self._is_attached:
            return
        logger.info(
            "[%s] Music Assistant: moved on to the next Qobuz track %s", self.name, armed.track_id
        )
        self._notify_next_track_started()
        self._notify_position_update(0)

    def _step_past_stale(self, item_id: str) -> None:
        """MA reached a next track the Qobuz app no longer wants (it was already buffered)."""
        if self._stale_task is not None and not self._stale_task.done():
            return
        armed = self._armed
        queue_id = self._player_id

        async def step_past() -> None:
            self._writes_in_flight += 1
            try:
                if armed is not None:
                    logger.info(
                        "[%s] Music Assistant reached a stale next track; jumping to %s",
                        self.name,
                        armed.track_id,
                    )
                    await self._connection.call(
                        f"player_queues/play_index queue={queue_id} item={armed.item_id}",
                        lambda c: c.player_queues.play_index(queue_id, armed.item_id),
                        timeout=PLAY_TIMEOUT_SECONDS,
                    )
                else:
                    logger.info(
                        "[%s] Music Assistant reached a stale next track; stopping it", self.name
                    )
                    await self._connection.call(
                        f"player_queues/stop queue={queue_id}",
                        lambda c: c.player_queues.stop(queue_id),
                    )
                    self._report_ended()
            except (NotConnectedError, MusicAssistantError) as e:
                logger.warning("[%s] Could not step past the stale track: %s", self.name, e)
            finally:
                # Stale until now: MA sends several events per track change, and one handled
                # before this step is done would read the item as "went back" and stop the app
                self._stale_item_ids.discard(item_id)
                self._writes_in_flight -= 1
                asyncio.get_running_loop().call_soon(self._check_queue)

        self._stale_task = self._run_in_background(step_past())

    def _report_ended(self) -> None:
        if self._has_reported_end:
            return
        self._has_reported_end = True
        self._is_attached = False
        self._queue_state = MusicAssistantState.IDLE
        self._cancel_timer("idle")
        logger.info("[%s] Music Assistant: track finished", self.name)
        self._notify_state_change(PlaybackState.STOPPED)
        self._notify_track_ended()

    def _report_interrupted(self) -> None:
        if self._has_reported_end:
            return
        self._has_reported_end = True
        self._is_attached = False
        self._queue_state = MusicAssistantState.IDLE
        self._cancel_timer("idle")
        self._notify_state_change(PlaybackState.STOPPED)
        self._notify_playback_interrupted(self._furthest_position_ms)

    def _let_go(self, reason: str) -> None:
        """Someone else uses the player now: let go, never fight back."""
        if self._has_external_playback:
            return
        self._has_external_playback = True
        self._is_attached = False
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
        if self._external_playback_callback is not None:
            self._let_go_task = self._run_in_background(self._external_playback_callback())

    def _claim(self, queue_item: QueueItem, metadata: BackendTrackMetadata) -> _OurItem:
        item = _OurItem(
            item_id=str(queue_item.queue_item_id),
            track_id=metadata.track_id,
            duration_ms=(
                int(queue_item.duration * 1000) if queue_item.duration else metadata.duration_ms
            ),
        )
        self._our_item_ids.add(item.item_id)
        self._stale_item_ids.discard(item.item_id)
        return item

    def _set_current(self, item: _OurItem, metadata: BackendTrackMetadata) -> None:
        self._current = item
        self._current_meta = metadata
        self._position_ms = 0
        self._position_at = time.monotonic()
        self._furthest_position_ms = 0
        self._has_played = False
        self._has_reported_end = False
        self._is_paused = False
        self._has_external_playback = False
        self._stall_since = None
        self._left_item_id = None
        self._play_started_at = time.monotonic()
        self._queue_state = MusicAssistantState.PLAYING
        self._expect(MusicAssistantState.PLAYING)
        self._save_session()

    def _forget_current(self) -> None:
        self._current = None
        self._current_meta = None
        self._armed = None
        self._is_attached = False
        self._save_session()

    async def _find_new_item(self, track_id: str, offset: int) -> Optional[QueueItem]:
        """Find the queue item MA just created for ``track_id`` (play_media returns nothing)."""
        queue_id = self._player_id
        items = await self._connection.call(
            f"player_queues/items queue={queue_id} offset={offset}",
            lambda c: c.player_queues.get_queue_items(queue_id, limit=20, offset=max(0, offset)),
        )
        for item in items:
            if item.queue_item_id not in self._our_item_ids and _is_track(item, track_id):
                return item
        return None

    async def _item_in_queue(self, item_id: str) -> bool:
        queue_id = self._player_id
        queue = self._connection.queue(queue_id)
        offset = (queue.current_index or 0) if queue is not None else 0
        items = await self._connection.call(
            f"player_queues/items queue={queue_id} offset={offset}",
            lambda c: c.player_queues.get_queue_items(queue_id, limit=20, offset=offset),
        )
        return any(item.queue_item_id == item_id for item in items)

    def _our_queue(self) -> Optional[PlayerQueue]:
        """MA's queue, but only while its current item is our current track."""
        if self._current is None:
            return None
        queue = self._connection.queue(self._player_id)
        if queue is None or queue.current_item is None:
            return None
        if queue.current_item.queue_item_id != self._current.item_id:
            return None
        return queue

    def _queue_plays_ours(self) -> bool:
        """Whether MA is playing or paused on our current (or queued next) item.

        MA describes the player from its own queue, so another app playing on
        the player directly (e.g. a WiiM's built-in Qobuz Connect) is not
        visible here; the stall check catches it later.
        """
        if self._current is None:
            return False
        queue = self._connection.queue(self._player_id)
        if queue is None or not queue.active or queue.current_item is None:
            return False
        ours = {self._current.item_id}
        if self._armed is not None:
            ours.add(self._armed.item_id)
        return queue.current_item.queue_item_id in ours and queue.state in _PLAYING_OR_PAUSED

    def _external_content_playing(self, queue: PlayerQueue) -> bool:
        if not queue.active:
            return True
        current = queue.current_item
        return (
            current is not None
            and current.queue_item_id not in self._our_item_ids
            and queue.state in _PLAYING_OR_PAUSED
        )

    def _update_position(self, queue: PlayerQueue, report_jumps: bool = False) -> None:
        now = time.monotonic()
        position = int(queue.corrected_elapsed_time * 1000)
        if self._current is not None and self._current.duration_ms > 0:
            position = min(position, self._current.duration_ms)
        position = max(0, position)
        # Where the track should be by now if nobody moved it
        expected = self._position_ms
        if self._queue_state == MusicAssistantState.PLAYING and self._position_at:
            expected += int((now - self._position_at) * 1000)
        if position < self._position_ms - 2000:
            self._furthest_position_ms = position  # moved back (seek or restart)
        self._position_ms = position
        self._position_at = now
        if queue.state == MusicAssistantState.PLAYING:
            self._has_played = True
            self._furthest_position_ms = max(self._furthest_position_ms, position)
        if (
            report_jumps
            and abs(position - expected) > 2000
            and queue.state in _PLAYING_OR_PAUSED
            and self._is_attached
            and not self._writes_in_flight
            and not self._is_in_grace()
            and now - self._last_seek_at >= SEEK_ECHO_SECONDS
        ):
            # Seek in MA's UI, or the player jumped by itself
            logger.info("[%s] Music Assistant: seek to %s", self.name, _format_ms(position))
            self._notify_position_update(position)

    def _is_near_end(self) -> bool:
        duration = self._current.duration_ms if self._current is not None else 0
        if duration <= 0 or self._furthest_position_ms <= 0:
            return True  # unknown: treat as an end so the queue never stalls
        return duration - self._furthest_position_ms <= END_MARGIN_MS

    def _expect(self, state: MusicAssistantState) -> None:
        """Our command should bring MA to ``state``; ignore MA's older reports until then."""
        self._expected_state = state
        self._expected_until = time.monotonic() + EXPECT_STATE_SECONDS

    def _pending_expectation(self) -> Optional[MusicAssistantState]:
        if self._expected_state is not None and time.monotonic() >= self._expected_until:
            self._expected_state = None
        return self._expected_state

    def _is_in_grace(self) -> bool:
        return self._grace_left() > 0

    def _grace_left(self) -> float:
        return START_GRACE_SECONDS - (time.monotonic() - self._play_started_at)

    def _check_queue_later(self, delay: float) -> None:
        if "check" not in self._timers:
            self._set_timer("check", max(0.05, delay), self._check_queue)

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

    def _run_in_background(self, coro: Awaitable[Any]) -> "asyncio.Future[Any]":
        task = asyncio.ensure_future(coro)
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)
        return task

    def _save_session(self) -> None:
        """Remember our MA items, so an add-on restart knows MA's current item is ours."""
        if self._current is None:
            self._save_session_callback(None)
            return
        session: dict[str, Any] = {"current": asdict(self._current)}
        if self._armed is not None:
            session["armed"] = asdict(self._armed)
        self._save_session_callback(session)

    def _restore_session(self) -> None:
        session = self._saved_session
        if not session:
            return
        try:
            current = _OurItem(**session["current"])
            armed = _OurItem(**session["armed"]) if session.get("armed") else None
        except (KeyError, TypeError) as e:
            logger.warning("[%s] Ignoring a saved session from another version: %s", self.name, e)
            return
        self._current, self._armed = current, armed
        self._our_item_ids.add(current.item_id)
        if armed is not None:
            self._is_armed_restored = True
            self._our_item_ids.add(armed.item_id)
        # Detached until the Qobuz app plays again: we only follow MA meanwhile.
        self._is_attached = False
        logger.info(
            "[%s] Remembered the last Qobuz track %s from before the restart",
            self.name,
            self._current.track_id,
        )
