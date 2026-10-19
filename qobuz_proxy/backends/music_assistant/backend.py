"""
Music Assistant audio backend.

Plays Qobuz tracks on one Music Assistant (MA) player or group through MA's own
Qobuz provider: the proxy tells MA "play qobuz://track/<id>" on the player's MA
queue (queue id == player id) and MA streams it, so MA's UI shows the track,
artwork, queue and progress. The streaming URL the player resolves is not used;
that lookup still serves as the region-availability check.

Ownership model (mirrors the DLNA backend): the MA queue items this backend
created are "ours". MA moving onto our armed next item is a gapless transition;
MA stopping, finishing or pausing our item is reported to the player; anything
else showing up on the queue means someone took the player over in MA, and the
backend hands it back without sending MA another command (no fight-back) until
the Qobuz app selects the device again.

MA pushes state over the websocket, so get_state()/get_position() read the
client's cache and never call MA.
"""

import asyncio
import logging
import time
from dataclasses import dataclass
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

from qobuz_proxy.backends.base import AudioBackend, ExternalPlaybackAware
from qobuz_proxy.backends.types import BackendInfo, BackendTrackMetadata, PlaybackState

from .connection import MAConnectionState, MAConnectionStatus, MANotConnectedError

logger = logging.getLogger(__name__)

# Ignore MA's transitional states this long after we started or moved a track.
START_GRACE_SECONDS = 4.0
# A stop with more than this left is someone stopping it, not the track ending.
EARLY_STOP_MARGIN_MS = 15_000
# Tell the app the track stopped if MA stays unreachable this long while playing.
DISCONNECT_GRACE_SECONDS = 20.0
# The player seeks right after play(); skip it when MA already starts there.
SEEK_TOLERANCE_MS = 2_500
# Don't adopt MA's current track right after we stopped it ourselves.
ADOPT_BLOCK_AFTER_STOP_SECONDS = 5.0
# Coalesce device-side volume changes before reporting them to the app.
VOLUME_DEBOUNCE_SECONDS = 0.3
# Wait before retrying to queue a next track after a transient MA error.
NEXT_RETRY_COOLDOWN_SECONDS = 5.0
# Starting at or below this position is a normal start, not a positioned one.
POSITIONED_START_MIN_MS = 2_000

_UNPLAYABLE_ERRORS = (MediaNotFoundError, UnplayableMediaError, InvalidProviderURI)


@dataclass
class _OwnItem:
    """An MA queue item this backend created."""

    item_id: str  # MA queue_item_id
    track_id: str  # Qobuz track id
    duration_ms: int
    title: str = ""
    qobuz_queue_item_id: int = 0  # Qobuz queue occurrence (armed next track only)


def _format_ms(ms: int) -> str:
    seconds = max(0, ms) // 1000
    return f"{seconds // 60}:{seconds % 60:02d}"


class MusicAssistantBackend(AudioBackend, ExternalPlaybackAware):
    """Audio backend for one Music Assistant player or group."""

    def __init__(
        self,
        connection: Any,
        player_id: str,
        name: Optional[str] = None,
        *,
        uri_template: str = "qobuz://track/{track_id}",
    ) -> None:
        super().__init__(name=name or player_id)
        self._conn = connection
        self._player_id = player_id
        self._uri_template = uri_template
        # Provider domain that marks a queue item as a Qobuz track ("qobuz")
        self._domain = uri_template.split("://", 1)[0]

        self._current: Optional[_OwnItem] = None
        self._current_meta: Optional[BackendTrackMetadata] = None
        self._armed: Optional[_OwnItem] = None
        self._own_item_ids: set[str] = set()
        self._stale_item_ids: set[str] = set()

        self._external = False
        self._starting = False
        self._stopping = False
        self._arming = False
        self._play_started_at = 0.0
        self._last_stop_at: Optional[float] = None
        self._ma_state: Optional[MAPlaybackState] = None
        self._paused_hold = False
        self._ended_fired = False
        self._stale_handling = False
        self._position_ms = 0
        self._furthest_position_ms = 0
        self._skip_seek_target: Optional[int] = None
        self._session_prepared = False

        self._failed_next: set[tuple[str, int]] = set()
        self._next_retry_after = 0.0
        self._gapless_supported = True

        self._pending_volume: Optional[int] = None
        self._volume_lock = asyncio.Lock()
        self._volume_timer: Optional[asyncio.TimerHandle] = None
        self._last_ma_volume: Optional[int] = None

        self._disconnect_timer: Optional[asyncio.TimerHandle] = None
        self._on_external_playback_cb: Optional[Callable[[], Awaitable[None]]] = None
        self._tasks: set[asyncio.Task[Any]] = set()
        self._unsubscribers: list[Callable[[], None]] = []

    # =========================================================================
    # Properties
    # =========================================================================

    @property
    def player_id(self) -> str:
        return self._player_id

    @property
    def supports_gapless(self) -> bool:
        return self._gapless_supported

    @property
    def reports_external_resume(self) -> bool:
        return True

    # =========================================================================
    # Lifecycle
    # =========================================================================

    async def connect(self) -> bool:
        """Subscribe to this player's MA events.

        Succeeds even while MA is unreachable: the Qobuz device stays visible
        and commands fail with a clear error until the connection is back.
        """
        if self._is_connected:
            return True
        self._unsubscribers = [
            self._conn.add_event_listener(self._player_id, self._on_ma_event),
            self._conn.add_state_listener(self._on_connection_state),
        ]
        self._is_connected = True
        player = self._conn.get_player(self._player_id)
        self._last_ma_volume = self._read_volume(player) if player is not None else None
        logger.info("[%s] Music Assistant backend ready (player %s)", self.name, self._player_id)
        return True

    async def disconnect(self) -> None:
        """Stop listening. MA keeps playing: an add-on restart must not stop the music."""
        if not self._is_connected:
            return
        self._is_connected = False
        for unsubscribe in self._unsubscribers:
            try:
                unsubscribe()
            except Exception:
                pass
        self._unsubscribers = []
        for timer in (self._disconnect_timer, self._volume_timer):
            if timer is not None:
                timer.cancel()
        self._disconnect_timer = None
        self._volume_timer = None
        for task in list(self._tasks):
            task.cancel()

    # =========================================================================
    # Playback control
    # =========================================================================

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
            raise MANotConnectedError("Music Assistant is not connected")
        if self._external:
            raise RuntimeError(
                "Another source is playing on this player in Music Assistant; "
                "select it again in the Qobuz app"
            )
        self._starting = True
        try:
            item = await self._adopt(metadata)
            if item is None:
                item = await self._jump_to_armed(metadata, start_ms)
            if item is None:
                item = await self._start_track(metadata, start_ms)
            self._set_current(item, metadata)
            if start_ms >= POSITIONED_START_MIN_MS:
                self._position_ms = start_ms
                self._furthest_position_ms = start_ms
                self._skip_seek_target = start_ms
            self._notify_state_change(PlaybackState.PLAYING)
        except MANotConnectedError:
            raise
        except _UNPLAYABLE_ERRORS as e:
            logger.error(
                "[%s] Music Assistant could not play Qobuz track %s ('%s'): %s. Is Qobuz set "
                "up in Music Assistant with an account that can play this track?",
                self.name,
                metadata.track_id,
                metadata.title,
                e,
            )
            raise RuntimeError(f"Music Assistant could not play track {metadata.track_id}") from e
        except PlayerUnavailableError as e:
            logger.error(
                "[%s] Music Assistant reports this player as unavailable: %s", self.name, e
            )
            raise RuntimeError("The Music Assistant player is unavailable") from e
        except MusicAssistantError as e:
            raise RuntimeError(f"Music Assistant refused to play: {e}") from e
        finally:
            self._starting = False
            # Let the player record the new state before we look at MA's events.
            asyncio.get_running_loop().call_soon(self._evaluate)

    async def _adopt(self, metadata: BackendTrackMetadata) -> Optional[_OwnItem]:
        """Take over MA's current item if it already plays this track.

        After an add-on restart (or a re-selection) the Qobuz app asks for the
        track MA is still playing; restarting it would be audible.
        """
        if self._current is not None:
            return None
        if (
            self._last_stop_at is not None
            and time.monotonic() - self._last_stop_at < ADOPT_BLOCK_AFTER_STOP_SECONDS
        ):
            return None
        queue = self._conn.get_queue(self._player_id)
        if queue is None or not queue.active or queue.current_item is None:
            return None
        if queue.state not in (MAPlaybackState.PLAYING, MAPlaybackState.PAUSED):
            return None
        if self._item_track_id(queue.current_item) != metadata.track_id:
            return None
        item = self._own_item(queue.current_item, metadata)
        logger.info(
            "[%s] Music Assistant is already playing track %s; taking it over without restarting",
            self.name,
            metadata.track_id,
        )
        if queue.state == MAPlaybackState.PAUSED:
            await self._conn.call(
                f"player_queues/play queue={self._player_id} (resume adopted track)",
                lambda c: c.player_queues.play(self._player_id),
            )
        position = int(queue.corrected_elapsed_time * 1000)
        self._skip_seek_target = position
        self._position_ms = position
        self._furthest_position_ms = position
        return item

    async def _jump_to_armed(
        self, metadata: BackendTrackMetadata, start_ms: int
    ) -> Optional[_OwnItem]:
        """Play the next track MA already has queued (a Qobuz "next")."""
        armed = self._armed
        if armed is None or armed.track_id != metadata.track_id:
            return None
        seek_s = start_ms // 1000 if start_ms >= POSITIONED_START_MIN_MS else 0
        try:
            await self._conn.call(
                f"player_queues/play_index queue={self._player_id} item={armed.item_id}",
                lambda c: c.player_queues.play_index(
                    self._player_id, armed.item_id, seek_position=seek_s
                ),
            )
        except InvalidDataError:
            # The queued item is gone (queue edited in MA); start the track afresh
            self._armed = None
            return None
        self._armed = None
        return armed

    async def _start_track(self, metadata: BackendTrackMetadata, start_ms: int) -> _OwnItem:
        """Replace the MA queue with this track, optionally starting mid-track."""
        await self._prepare_session()
        uri = self._uri(metadata.track_id)
        qid = self._player_id
        self._armed = None
        if start_ms < POSITIONED_START_MIN_MS:
            await self._conn.call(
                f"player_queues/play_media queue={qid} media={uri} option=replace",
                lambda c: c.player_queues.play_media(qid, media=uri, option=QueueOption.REPLACE),
            )
            queue = self._conn.get_queue(qid)
            current = queue.current_item if queue is not None else None
            if (
                current is not None
                and current.queue_item_id not in self._own_item_ids
                and self._item_track_id(current) == metadata.track_id
            ):
                return self._own_item(current, metadata)
            found = await self._find_new_item(metadata.track_id, offset=0)
        else:
            # One stream start at the right spot instead of play-then-seek
            await self._conn.call(
                f"player_queues/clear queue={qid}", lambda c: c.player_queues.clear(qid)
            )
            await self._conn.call(
                f"player_queues/play_media queue={qid} media={uri} option=add",
                lambda c: c.player_queues.play_media(qid, media=uri, option=QueueOption.ADD),
            )
            found = await self._find_new_item(metadata.track_id, offset=0)
            if found is not None:
                seek_s = start_ms // 1000
                item_id = found.queue_item_id
                await self._conn.call(
                    f"player_queues/play_index queue={qid} item={item_id} seek={seek_s}s",
                    lambda c: c.player_queues.play_index(qid, item_id, seek_position=seek_s),
                )
        if found is None:
            raise MediaNotFoundError(
                f"Music Assistant did not queue Qobuz track {metadata.track_id}"
            )
        return self._own_item(found, metadata)

    async def _prepare_session(self) -> None:
        """Turn off MA queue settings that would fight the Qobuz queue (once per selection)."""
        if self._session_prepared:
            return
        self._session_prepared = True
        queue = self._conn.get_queue(self._player_id)
        if queue is None:
            return
        qid = self._player_id
        if getattr(queue, "repeat_mode", RepeatMode.OFF) not in (RepeatMode.OFF, None):
            try:
                await self._conn.call(
                    f"player_queues/repeat queue={qid} repeat_mode=off",
                    lambda c: c.player_queues.repeat(qid, RepeatMode.OFF),
                )
                logger.info(
                    "[%s] Turned off repeat in Music Assistant so the Qobuz queue decides "
                    "what plays next",
                    self.name,
                )
            except MusicAssistantError as e:
                logger.info("[%s] Could not turn off repeat in Music Assistant: %s", self.name, e)
        autoplay = getattr(queue, "autoplay_enabled", None)
        if autoplay is None:
            autoplay = getattr(queue, "dont_stop_the_music_enabled", False)
        if autoplay:
            try:
                await self._conn.call(
                    f"player_queues/autoplay queue={qid} autoplay_enabled=false",
                    lambda c: c.send_command(
                        "player_queues/autoplay", queue_id=qid, autoplay_enabled=False
                    ),
                )
            except MusicAssistantError:
                try:
                    await self._conn.call(
                        f"player_queues/dont_stop_the_music queue={qid} enabled=false",
                        lambda c: c.player_queues.dont_stop_the_music(qid, False),
                    )
                except MusicAssistantError as e:
                    logger.info("[%s] Could not turn off autoplay: %s", self.name, e)
                    return
            logger.info(
                "[%s] Turned off autoplay in Music Assistant so the album ends when the Qobuz "
                "queue ends",
                self.name,
            )

    async def pause(self) -> None:
        logger.info("[%s] Qobuz: pause", self.name)
        if not self._conn.is_connected:
            raise MANotConnectedError("Music Assistant is not connected")
        if self._ma_plays_ours():
            await self._conn.call(
                f"player_queues/pause queue={self._player_id}",
                lambda c: c.player_queues.pause(self._player_id),
            )
        self._paused_hold = True
        self._notify_state_change(PlaybackState.PAUSED)

    async def resume(self) -> bool:
        logger.info("[%s] Qobuz: resume", self.name)
        if not self._conn.is_connected or self._external or self._current is None:
            return False
        current = self._current
        queue = self._conn.get_queue(self._player_id)
        if (
            queue is not None
            and queue.active
            and queue.current_item is not None
            and queue.current_item.queue_item_id == current.item_id
        ):
            if queue.state == MAPlaybackState.PLAYING:
                self._paused_hold = False
                self._notify_state_change(PlaybackState.PLAYING)
                return True
            if queue.state == MAPlaybackState.PAUSED or (
                queue.state == MAPlaybackState.IDLE and not queue.ended
            ):
                try:
                    await self._conn.call(
                        f"player_queues/play queue={self._player_id}",
                        lambda c: c.player_queues.play(self._player_id),
                    )
                except (MANotConnectedError, MusicAssistantError):
                    return False
                self._paused_hold = False
                self._ma_state = MAPlaybackState.PLAYING
                self._play_started_at = time.monotonic()
                self._notify_state_change(PlaybackState.PLAYING)
                return True
        if queue is not None and self._foreign_content_playing(queue):
            self._takeover("Music Assistant is playing something else")
            return False
        # Our item is gone or finished (e.g. MA cleared it): start it again where we were
        if self._current_meta is None:
            return False
        meta = BackendTrackMetadata(
            **{**self._current_meta.__dict__, "start_position_ms": self._position_ms}
        )
        self._current = None
        try:
            await self.play("", meta)
        except Exception as e:
            logger.warning("[%s] Could not resume in Music Assistant: %s", self.name, e)
            return False
        return True

    async def stop(self, *, next_track_id: Optional[str] = None) -> None:
        armed = self._armed
        if next_track_id is not None and armed is not None and armed.track_id == next_track_id:
            # play() jumps straight to the item MA already has queued
            logger.info("[%s] Qobuz: stop (switching to the queued next track)", self.name)
            self._paused_hold = False
            self._notify_state_change(PlaybackState.STOPPED)
            return
        logger.info("[%s] Qobuz: stop", self.name)
        if self._ma_plays_ours():
            self._stopping = True
            try:
                await self._conn.call(
                    f"player_queues/stop queue={self._player_id}",
                    lambda c: c.player_queues.stop(self._player_id),
                )
                self._last_stop_at = time.monotonic()
                self._ma_state = MAPlaybackState.IDLE
            except (MANotConnectedError, MusicAssistantError) as e:
                logger.warning("[%s] Music Assistant did not stop: %s", self.name, e)
            finally:
                self._stopping = False
        if armed is not None:
            self._stale_item_ids.add(armed.item_id)
        self._armed = None
        self._paused_hold = False
        self._notify_state_change(PlaybackState.STOPPED)

    # =========================================================================
    # Position control
    # =========================================================================

    async def seek(self, position_ms: int) -> None:
        target = self._skip_seek_target
        self._skip_seek_target = None
        if (
            target is not None
            and abs(position_ms - target) <= SEEK_TOLERANCE_MS
            and time.monotonic() - self._play_started_at < START_GRACE_SECONDS
        ):
            logger.debug("[%s] Already at %s; skipping seek", self.name, _format_ms(position_ms))
            return
        logger.info("[%s] Qobuz: seek to %s", self.name, _format_ms(position_ms))
        if not self._conn.is_connected:
            raise MANotConnectedError("Music Assistant is not connected")
        if not self._ma_plays_ours():
            raise RuntimeError("Cannot seek: Music Assistant is not playing this Qobuz track")
        seconds = int(round(position_ms / 1000))
        await self._conn.call(
            f"player_queues/seek queue={self._player_id} position={seconds}s",
            lambda c: c.player_queues.seek(self._player_id, seconds),
        )
        # MA restarts the stream to seek; don't read that as a stop
        self._play_started_at = time.monotonic()
        self._position_ms = position_ms
        self._furthest_position_ms = position_ms

    async def get_position(self) -> int:
        queue = self._our_queue()
        if queue is not None and self._current is not None:
            position = int(queue.corrected_elapsed_time * 1000)
            if self._current.duration_ms > 0:
                position = min(position, self._current.duration_ms)
            self._position_ms = max(0, position)
            if queue.state == MAPlaybackState.PLAYING:
                self._furthest_position_ms = max(self._furthest_position_ms, self._position_ms)
        return self._position_ms

    # =========================================================================
    # Volume
    # =========================================================================

    async def set_volume(self, level: int) -> None:
        level = max(0, min(100, level))
        if self._external:
            logger.info("[%s] Qobuz: volume %s%% ignored (not our player now)", self.name, level)
            return
        self._volume = level
        self._pending_volume = level
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
            player = self._conn.get_player(self._player_id)
            if player is not None and player.type == PlayerType.GROUP:
                await self._conn.call(
                    f"players/cmd/group_volume player={self._player_id} level={level}",
                    lambda c: c.players.group_volume(self._player_id, level),
                )
            else:
                await self._conn.call(
                    f"players/cmd/volume_set player={self._player_id} level={level}",
                    lambda c: c.players.volume_set(self._player_id, level),
                )

    async def get_volume(self) -> int:
        player = self._conn.get_player(self._player_id)
        level = self._read_volume(player) if player is not None else None
        if level is not None:
            self._volume = level
        return self._volume

    def _read_volume(self, player: Any) -> Optional[int]:
        if player.type == PlayerType.GROUP:
            value = getattr(player, "group_volume", None)
        else:
            value = getattr(player, "volume_level", None)
        return int(value) if isinstance(value, (int, float)) else None

    # =========================================================================
    # State
    # =========================================================================

    async def get_state(self) -> PlaybackState:
        if self._starting:
            return PlaybackState.PLAYING
        if self._current is None:
            return PlaybackState.STOPPED
        if not self._conn.is_connected:
            return self._state  # frozen until MA is back
        queue = self._our_queue()
        if queue is None:
            return PlaybackState.STOPPED
        if queue.state == MAPlaybackState.PLAYING:
            return PlaybackState.PLAYING
        if queue.state == MAPlaybackState.PAUSED:
            return PlaybackState.PAUSED
        return PlaybackState.PAUSED if self._paused_hold else PlaybackState.STOPPED

    # =========================================================================
    # Gapless
    # =========================================================================

    async def set_next_track(
        self, url: str, metadata: BackendTrackMetadata, queue_item_id: int = 0
    ) -> bool:
        """Queue the next Qobuz track right after the current one in MA."""
        key = (metadata.track_id, queue_item_id)
        armed = self._armed
        if (
            armed is not None
            and (armed.track_id, armed.qobuz_queue_item_id) == key
            and armed.item_id not in self._stale_item_ids
        ):
            return True
        if key in self._failed_next or time.monotonic() < self._next_retry_after:
            return False
        if self._arming or not self._conn.is_connected or not self._ma_plays_ours():
            return False
        logger.info("[%s] Qobuz: queue next track %s '%s'", self.name, *key[:1], metadata.title)
        qid = self._player_id
        uri = self._uri(metadata.track_id)
        self._arming = True
        try:
            if armed is not None:
                # REPLACE_NEXT drops it unless the speaker already buffered it
                self._stale_item_ids.add(armed.item_id)
                self._armed = None
            queue = self._conn.get_queue(qid)
            offset = (queue.current_index or 0) if queue is not None else 0
            await self._conn.call(
                f"player_queues/play_media queue={qid} media={uri} option=replace_next",
                lambda c: c.player_queues.play_media(
                    qid, media=uri, option=QueueOption.REPLACE_NEXT
                ),
            )
            found = await self._find_new_item(metadata.track_id, offset=offset)
            if found is None:
                raise MediaNotFoundError(f"Music Assistant did not queue track {metadata.track_id}")
            item = self._own_item(found, metadata)
            item.qobuz_queue_item_id = queue_item_id
            self._armed = item
            logger.info(
                "[%s] Queued next track %s in Music Assistant (gapless)",
                self.name,
                metadata.track_id,
            )
            return True
        except _UNPLAYABLE_ERRORS as e:
            self._failed_next.add(key)
            logger.warning(
                "[%s] Music Assistant cannot queue track %s: %s (it will be skipped or played "
                "without gapless)",
                self.name,
                metadata.track_id,
                e,
            )
            return False
        except (MANotConnectedError, MusicAssistantError) as e:
            self._next_retry_after = time.monotonic() + NEXT_RETRY_COOLDOWN_SECONDS
            logger.warning("[%s] Could not queue the next track: %s", self.name, e)
            return False
        finally:
            self._arming = False
            # Runs after the player recorded the armed track
            asyncio.get_running_loop().call_soon(self._evaluate)

    async def clear_next_track(self) -> None:
        armed = self._armed
        if armed is None:
            return
        self._armed = None
        # If MA reaches it anyway (already buffered), we step past it
        self._stale_item_ids.add(armed.item_id)
        if not self._conn.is_connected or self._external:
            return
        logger.info(
            "[%s] Qobuz: next track changed; removing %s from MA", self.name, armed.track_id
        )
        try:
            await self._conn.call(
                f"player_queues/delete_item queue={self._player_id} item={armed.item_id}",
                lambda c: c.player_queues.delete_item(self._player_id, armed.item_id),
            )
            if not await self._item_in_queue(armed.item_id):
                self._stale_item_ids.discard(armed.item_id)
        except (MANotConnectedError, MusicAssistantError) as e:
            logger.debug("[%s] Could not remove the queued next track: %s", self.name, e)

    # =========================================================================
    # External playback (another source took over)
    # =========================================================================

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
        return self._external

    async def can_apply_remote_state(self) -> bool:
        """Refuse replayed Qobuz snapshots while MA plays something else."""
        if self._external:
            return False
        if self._current is None or not self._conn.is_connected:
            return True
        if self._in_grace() or self._ma_plays_ours():
            return True
        queue = self._conn.get_queue(self._player_id)
        return queue is None or not self._foreign_content_playing(queue)

    def get_info(self) -> BackendInfo:
        return BackendInfo(
            backend_type="music_assistant",
            name=self.name,
            device_id=self._player_id,
        )

    # =========================================================================
    # MA events
    # =========================================================================

    def _on_ma_event(self, event: Any) -> None:
        try:
            if event.event == EventType.PLAYER_UPDATED:
                self._check_device_volume()
            elif event.event == EventType.QUEUE_TIME_UPDATED:
                self._check_position_jump()
            if event.event in (
                EventType.QUEUE_UPDATED,
                EventType.QUEUE_ADDED,
                EventType.PLAYER_UPDATED,
                EventType.QUEUE_TIME_UPDATED,
            ):
                self._evaluate()
        except Exception as e:
            logger.error("[%s] Error handling a Music Assistant event: %s", self.name, e)

    def _on_connection_state(self, status: MAConnectionStatus) -> None:
        try:
            if status.state == MAConnectionState.CONNECTED:
                if self._disconnect_timer is not None:
                    self._disconnect_timer.cancel()
                    self._disconnect_timer = None
                player = self._conn.get_player(self._player_id)
                if player is not None:
                    self._last_ma_volume = self._read_volume(player)
                # Resync: MA may have stopped (restart) or moved on while we were away
                self._evaluate()
            elif (
                self._current is not None
                and self._state == PlaybackState.PLAYING
                and self._disconnect_timer is None
            ):
                loop = asyncio.get_running_loop()
                self._disconnect_timer = loop.call_later(
                    DISCONNECT_GRACE_SECONDS, self._on_disconnect_grace_expired
                )
        except Exception as e:
            logger.error("[%s] Error handling the Music Assistant connection: %s", self.name, e)

    def _on_disconnect_grace_expired(self) -> None:
        self._disconnect_timer = None
        if self._conn.is_connected or self._current is None or self._external:
            return
        if self._state != PlaybackState.PLAYING:
            return
        logger.warning(
            "[%s] Lost Music Assistant; showing the track as stopped in the Qobuz app", self.name
        )
        self._ma_state = MAPlaybackState.IDLE
        self._notify_state_change(PlaybackState.STOPPED)
        self._notify_playback_interrupted(self._position_ms)

    def _check_device_volume(self) -> None:
        player = self._conn.get_player(self._player_id)
        if player is None:
            return
        level = self._read_volume(player)
        if level is None or level == self._last_ma_volume:
            return
        self._last_ma_volume = level
        if level == self._volume:
            return  # our own change echoed back
        self._volume = level
        if self._volume_timer is not None:
            self._volume_timer.cancel()
        loop = asyncio.get_running_loop()
        self._volume_timer = loop.call_later(VOLUME_DEBOUNCE_SECONDS, self._report_device_volume)

    def _report_device_volume(self) -> None:
        self._volume_timer = None
        if self._external:
            return
        logger.info("[%s] MA: volume changed to %s%% in Music Assistant", self.name, self._volume)
        self._notify_volume_change(self._volume)

    def _check_position_jump(self) -> None:
        queue = self._our_queue()
        if queue is None or self._starting:
            return
        position = int(queue.corrected_elapsed_time * 1000)
        if abs(position - self._position_ms) > 2000:
            logger.info("[%s] MA: seek to %s in Music Assistant", self.name, _format_ms(position))
            self._position_ms = position
            self._furthest_position_ms = position
            self._notify_position_update(position)

    def _evaluate(self) -> None:
        """Map MA's queue state onto the player callbacks (sync; never awaits)."""
        if (
            self._starting
            or self._stopping
            or self._current is None
            or self._external
            or not self._is_connected
            or not self._conn.is_connected
        ):
            return
        queue = self._conn.get_queue(self._player_id)
        if queue is None:
            return
        extra = getattr(queue, "extra_attributes", None) or {}
        if extra.get("play_action_in_progress"):
            return  # MA is switching tracks; wait for the settled state
        current = self._current
        cur_item = queue.current_item
        cur_id = cur_item.queue_item_id if cur_item is not None else None
        in_grace = self._in_grace()

        if cur_id == current.item_id:
            if not queue.active:
                if not in_grace:
                    self._takeover("the player switched to another source in Music Assistant")
                return
            self._update_position(queue)
            state = queue.state
            if state == MAPlaybackState.PAUSED:
                if not self._paused_hold:
                    logger.info("[%s] MA: paused in Music Assistant", self.name)
                self._paused_hold = True
            elif state == MAPlaybackState.PLAYING:
                if self._paused_hold and self._ma_state != MAPlaybackState.PLAYING:
                    logger.info("[%s] MA: playing again in Music Assistant", self.name)
                self._paused_hold = False
            if (
                self._ma_state == MAPlaybackState.PLAYING
                and state == MAPlaybackState.IDLE
                and not in_grace
                and not self._paused_hold
            ):
                if queue.ended or self._near_end():
                    self._fire_track_ended()
                else:
                    logger.info(
                        "[%s] MA: stopped in Music Assistant at %s",
                        self.name,
                        _format_ms(self._furthest_position_ms),
                    )
                    self._notify_state_change(PlaybackState.STOPPED)
                    self._notify_playback_interrupted(self._furthest_position_ms)
            self._ma_state = state
            return

        armed = self._armed
        if armed is not None and cur_id == armed.item_id:
            self._promote_armed(armed, queue)
            return

        if cur_id is not None and cur_id in self._stale_item_ids:
            self._handle_stale_arrival()
            return

        if in_grace or self._arming:
            return
        if self._state not in (PlaybackState.PLAYING, PlaybackState.PAUSED):
            # Nothing of ours is live; just forget it, nothing to hand back
            if cur_id is not None or queue.state != MAPlaybackState.IDLE:
                logger.info("[%s] MA: Music Assistant moved on to something else", self.name)
                self._current = None
                self._current_meta = None
                self._armed = None
            return
        if cur_id is None:
            if queue.state == MAPlaybackState.IDLE:
                # Queue cleared in MA while we were playing: treat as a stop
                logger.info("[%s] MA: queue cleared in Music Assistant", self.name)
                self._ma_state = MAPlaybackState.IDLE
                self._notify_state_change(PlaybackState.STOPPED)
                self._notify_playback_interrupted(self._furthest_position_ms)
            return
        name = getattr(cur_item, "name", "") or "another item"
        self._takeover(f"Music Assistant is now playing something else ({name})")

    def _promote_armed(self, armed: _OwnItem, queue: Any) -> None:
        """MA moved onto the next track we queued (gapless, or "next" in MA)."""
        logger.info("[%s] MA: moved on to the next Qobuz track %s", self.name, armed.track_id)
        self._current = armed
        self._armed = None
        self._position_ms = 0
        self._furthest_position_ms = 0
        self._ended_fired = False
        self._paused_hold = queue.state == MAPlaybackState.PAUSED
        self._play_started_at = time.monotonic()
        self._ma_state = queue.state
        self._notify_next_track_started()
        self._notify_position_update(0)

    def _handle_stale_arrival(self) -> None:
        """MA reached a next track the Qobuz app no longer wants (it was already buffered)."""
        if self._stale_handling:
            return
        self._stale_handling = True
        armed = self._armed

        async def step_past() -> None:
            try:
                if armed is not None:
                    logger.info(
                        "[%s] MA reached an outdated next track; jumping to %s",
                        self.name,
                        armed.track_id,
                    )
                    await self._conn.call(
                        f"player_queues/play_index queue={self._player_id} item={armed.item_id}",
                        lambda c: c.player_queues.play_index(self._player_id, armed.item_id),
                    )
                else:
                    logger.info("[%s] MA reached an outdated next track; stopping it", self.name)
                    self._stopping = True
                    try:
                        await self._conn.call(
                            f"player_queues/stop queue={self._player_id}",
                            lambda c: c.player_queues.stop(self._player_id),
                        )
                    finally:
                        self._stopping = False
                    self._fire_track_ended()
            except (MANotConnectedError, MusicAssistantError) as e:
                logger.warning("[%s] Could not step past the outdated track: %s", self.name, e)
            finally:
                self._stale_handling = False

        self._spawn(step_past())

    def _fire_track_ended(self) -> None:
        if self._ended_fired:
            return
        self._ended_fired = True
        logger.info("[%s] MA: track finished", self.name)
        self._ma_state = MAPlaybackState.IDLE
        self._notify_state_change(PlaybackState.STOPPED)
        self._notify_track_ended()

    def _takeover(self, reason: str) -> None:
        """Someone else uses the player now: hand it back, never fight."""
        if self._external:
            return
        self._external = True
        self._armed = None
        logger.warning(
            "[%s] %s — handing the player back to Music Assistant and leaving the Qobuz "
            "session (no fight-back)",
            self.name,
            reason,
        )
        self._notify_state_change(PlaybackState.STOPPED)
        if self._on_external_playback_cb is not None:
            self._spawn(self._on_external_playback_cb())

    # =========================================================================
    # Helpers
    # =========================================================================

    def _uri(self, track_id: str) -> str:
        return self._uri_template.format(track_id=track_id)

    def _item_track_id(self, item: Any) -> Optional[str]:
        """The Qobuz track id behind an MA queue item (library items included)."""
        media = getattr(item, "media_item", None)
        if media is None:
            return None
        provider = str(getattr(media, "provider", ""))
        if provider == self._domain or provider.startswith(f"{self._domain}--"):
            return str(media.item_id)
        for mapping in getattr(media, "provider_mappings", None) or []:
            if getattr(mapping, "provider_domain", "") == self._domain:
                return str(mapping.item_id)
        return None

    def _own_item(self, queue_item: Any, metadata: BackendTrackMetadata) -> _OwnItem:
        duration_s = getattr(queue_item, "duration", None)
        duration_ms = int(duration_s * 1000) if duration_s else metadata.duration_ms
        item = _OwnItem(
            item_id=str(queue_item.queue_item_id),
            track_id=metadata.track_id,
            duration_ms=duration_ms,
            title=metadata.title,
        )
        self._own_item_ids.add(item.item_id)
        self._stale_item_ids.discard(item.item_id)
        return item

    def _set_current(self, item: _OwnItem, metadata: BackendTrackMetadata) -> None:
        self._current = item
        self._current_meta = metadata
        self._position_ms = 0
        self._furthest_position_ms = 0
        self._ended_fired = False
        self._paused_hold = False
        self._external = False
        self._play_started_at = time.monotonic()
        self._ma_state = MAPlaybackState.PLAYING

    async def _find_new_item(self, track_id: str, offset: int) -> Optional[Any]:
        """Find the queue item MA just created for ``track_id`` (play_media returns nothing)."""
        qid = self._player_id
        items = await self._conn.call(
            f"player_queues/items queue={qid} offset={offset}",
            lambda c: c.player_queues.get_queue_items(qid, limit=20, offset=max(0, offset)),
        )
        for item in items:
            if item.queue_item_id in self._own_item_ids:
                continue
            if self._item_track_id(item) == track_id:
                return item
        return None

    async def _item_in_queue(self, item_id: str) -> bool:
        queue = self._conn.get_queue(self._player_id)
        offset = (queue.current_index or 0) if queue is not None else 0
        items = await self._conn.call(
            f"player_queues/items queue={self._player_id} offset={offset}",
            lambda c: c.player_queues.get_queue_items(self._player_id, limit=20, offset=offset),
        )
        return any(item.queue_item_id == item_id for item in items)

    def _our_queue(self) -> Optional[Any]:
        """MA's queue, but only while its current item is our current track."""
        if self._current is None or not self._conn.is_connected:
            return None
        queue = self._conn.get_queue(self._player_id)
        if queue is None or queue.current_item is None:
            return None
        if queue.current_item.queue_item_id != self._current.item_id:
            return None
        return queue

    def _ma_plays_ours(self) -> bool:
        """Whether MA is playing or paused on our current (or armed) item."""
        if self._current is None or not self._conn.is_connected:
            return False
        queue = self._conn.get_queue(self._player_id)
        if queue is None or not queue.active or queue.current_item is None:
            return False
        ours = {self._current.item_id}
        if self._armed is not None:
            ours.add(self._armed.item_id)
        if queue.current_item.queue_item_id not in ours:
            return False
        if queue.state not in (MAPlaybackState.PLAYING, MAPlaybackState.PAUSED):
            return False
        player = self._conn.get_player(self._player_id)
        media = getattr(player, "current_media", None) if player is not None else None
        media_item_id = getattr(media, "queue_item_id", None) if media is not None else None
        if media_item_id and media_item_id not in self._own_item_ids:
            # The speaker plays something MA did not queue for us (e.g. its own Qobuz Connect)
            return False
        return True

    def _foreign_content_playing(self, queue: Any) -> bool:
        if not queue.active:
            return True
        cur = queue.current_item
        return (
            cur is not None
            and cur.queue_item_id not in self._own_item_ids
            and queue.state in (MAPlaybackState.PLAYING, MAPlaybackState.PAUSED)
        )

    def _update_position(self, queue: Any) -> None:
        position = int(queue.corrected_elapsed_time * 1000)
        if self._current is not None and self._current.duration_ms > 0:
            position = min(position, self._current.duration_ms)
        self._position_ms = max(0, position)
        if queue.state == MAPlaybackState.PLAYING:
            self._furthest_position_ms = max(self._furthest_position_ms, self._position_ms)

    def _near_end(self) -> bool:
        duration = self._current.duration_ms if self._current is not None else 0
        if duration <= 0 or self._furthest_position_ms <= 0:
            return True  # unknown: treat as an end so the queue never stalls
        return duration - self._furthest_position_ms <= EARLY_STOP_MARGIN_MS

    def _in_grace(self) -> bool:
        return time.monotonic() - self._play_started_at < START_GRACE_SECONDS

    def _spawn(self, coro: Awaitable[Any]) -> None:
        task = asyncio.ensure_future(coro)
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)
