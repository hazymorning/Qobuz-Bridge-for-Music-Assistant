"""In-memory Music Assistant 2.10.7 server that drives the real ``music-assistant-client``.

:class:`FakeMusicAssistantServer` keeps what a real MA server keeps (players, queues, queue items,
providers, tokens) and outlives the single-use clients the product builds on every reconnect.
Its queue logic is a port of MA 2.10.7 ``controllers/player_queues`` and it emits the same events
in the same order. Only the websocket is fake: :class:`FakeMusicAssistantClient` is the real
client with :class:`FakeWebsocketsConnection` swapped in, so the product's error handling and
event races run against real client code.

Time never passes on its own: ``advance_time``, ``finish_current_track``, ``mark_ended`` and
``auto_stop_after_pause`` stand in for MA's timers, and ``settle()`` only yields to the event
loop, so tests patch the product's timing constants.
"""

from __future__ import annotations

import asyncio
import inspect
import time
from collections import deque
from collections.abc import Iterable, Iterator
from contextlib import contextmanager
from typing import Any, Callable, ClassVar, Optional, Union
from urllib.parse import urlparse

import aiohttp
from music_assistant_client import MusicAssistantClient
from music_assistant_client.connection import get_websocket_url
from music_assistant_client.exceptions import (
    CannotConnect,
    ConnectionClosed,
    ConnectionFailed,
    NotConnected,
)
from music_assistant_client.helpers import json_dumps, json_loads
from music_assistant_models.api import ServerInfoMessage, SuccessResultMessage
from music_assistant_models.auth import User
from music_assistant_models.enums import (
    CoreState,
    EventType,
    MediaType,
    PlaybackState,
    PlayerType,
    ProviderType,
    QueueOption,
    RepeatMode,
)
from music_assistant_models.errors import (
    InsufficientPermissions,
    InvalidCommand,
    InvalidDataError,
    InvalidToken,
    LoginFailed,
    MediaNotFoundError,
    PlayerUnavailableError,
    ProviderUnavailableError,
    QueueEmpty,
)
from music_assistant_models.event import MassEvent
from music_assistant_models.media_items import (
    ItemMapping,
    MediaItem,
    ProviderMapping,
    Radio,
    Track,
)
from music_assistant_models.player import DeviceInfo, Player, PlayerMedia
from music_assistant_models.player_queue import PlayerQueue
from music_assistant_models.provider import ProviderInstance
from music_assistant_models.queue_item import QueueItem
from music_assistant_models.unique_list import UniqueList

SCHEMA_VERSION = 65  # MA 2.10.7 constants.API_SCHEMA_VERSION
MIN_SCHEMA_VERSION = 28  # MA 2.10.7 constants.MIN_SCHEMA_VERSION
TRACK_DURATION = 200  # seconds, every track
ATTR_PLAY_ACTION_IN_PROGRESS = "play_action_in_progress"
# Commands that only read state: ``FakeMusicAssistantServer.writes`` leaves them out
READ_COMMANDS = frozenset(
    {
        "providers",
        "providers/manifests",
        "players/all",
        "player_queues/all",
        "player_queues/items",
        "dashboard/dashboards",
        "dashboard/sessions",
    }
)
# ``fail_next`` values besides exceptions
DROP = "drop"  # close the socket instead of answering (start_listening returns None)
HANG = "hang"  # never answer

_CLOSE = object()  # close frame (ConnectionClosed on the client)
_ERROR = object()  # websocket error (ConnectionFailed on the client)


class FakeMusicAssistantServer:
    """A Music Assistant 2.10.7 server that outlives the clients connecting to it.

    State a test may read or set: ``players``, ``queues`` and ``queue_items`` (live models),
    ``providers``, ``users`` (username -> password), ``guest_users``, ``reachable``, ``aliases``
    (other addresses that reach it), ``setup_required`` (a fresh MA: the websocket closes and
    login answers 403), ``min_schema_version``, ``server_version``.
    Test inputs: ``fail_next[command]`` (one shot: an exception to answer with, ``DROP`` or
    ``HANG``), ``unplayable_track_ids`` (resolve as unavailable, so play_media leaves them out),
    ``library_track_ids`` (come back as library items with the Qobuz id only in the provider
    mapping), ``dead_player_ids`` (speakers that never start playing).
    Logs: ``command_log`` (``(command, args)`` of every command but ``auth``; args are JSON, and
    enums equal their ``StrEnum`` members) and ``created_clients``.
    """

    def __init__(self) -> None:
        self.url = "http://127.0.0.1:8095"
        self.aliases: set[str] = set()
        self.token = "fake-ma-long-lived-token"
        self.server_version = "2.10.7"
        self.min_schema_version = MIN_SCHEMA_VERSION
        self.users = {"admin": "admin-password"}
        self.guest_users: set[str] = set()
        self.reachable = True
        self.setup_required = False

        self.players: dict[str, Player] = {}
        self.queues: dict[str, PlayerQueue] = {}
        self.queue_items: dict[str, list[QueueItem]] = {}
        self.providers: list[ProviderInstance] = []
        self.unplayable_track_ids: set[str] = set()
        self.library_track_ids: set[str] = set()
        self.dead_player_ids: set[str] = set()

        self.fail_next: dict[str, Any] = {}
        self.command_log: list[tuple[str, dict[str, Any]]] = []
        self.created_clients: list[FakeMusicAssistantClient] = []
        # ``MusicAssistantClient`` replacement bound to this server (same signature)
        self.client_class: type[FakeMusicAssistantClient] = type(
            "FakeMusicAssistantClient", (FakeMusicAssistantClient,), {"server": self}
        )

        self._commands = self._command_table()
        self._tasks: set[asyncio.Task[None]] = set()
        self._gates: dict[str, asyncio.Event] = {}
        self._gate_waiters = 0
        self._token_users = {self.token: "admin"}  # token -> username
        self._token_seq = 0
        self._item_seq = 0
        self._library_ids: dict[str, str] = {}
        self._op_depth = 0
        self._dirty_players: set[str] = set()
        self._player_fingerprints: dict[str, dict[str, Any]] = {}
        self._device_item: dict[str, Optional[str]] = {}  # queue item the speaker plays
        self._prev_state: dict[str, Optional[tuple[PlaybackState, Optional[str], int]]] = {}
        self._busy: dict[str, int] = {}  # play actions running per queue
        self._transitioning: dict[str, bool] = {}

    def client_factory(
        self, server_url: str, aiohttp_session: aiohttp.ClientSession, token: str
    ) -> FakeMusicAssistantClient:
        """The product's ``client_factory``: a new client bound to this server."""
        return self.client_class(server_url, aiohttp_session, token)

    @property
    def clients(self) -> list[FakeMusicAssistantClient]:
        """Clients whose websocket is open, as the server sees it."""
        return [
            client
            for client in self.created_clients
            if client.connection.socket is not None and not client.connection.socket.closed
        ]

    @property
    def valid_tokens(self) -> set[str]:
        return set(self._token_users)

    def calls(self, command: str) -> list[dict[str, Any]]:
        """Args of every logged ``command``, oldest first."""
        return [args for name, args in self.command_log if name == command]

    @property
    def writes(self) -> list[tuple[str, dict[str, Any]]]:
        """``command_log`` without the read-only commands: what clients asked MA to *do*."""
        return [(name, args) for name, args in self.command_log if name not in READ_COMMANDS]

    def clear_logs(self) -> None:
        self.command_log.clear()

    def queue(self, queue_id: str) -> PlayerQueue:
        return self.queues[queue_id]

    def item_ids(self, queue_id: str) -> list[str]:
        """``queue_item_id`` of every item, in queue order."""
        return [item.queue_item_id for item in self.queue_items[queue_id]]

    def track_ids(self, queue_id: str) -> list[str]:
        """Provider track id of every item, in queue order."""
        return [_item_track_id(item) for item in self.queue_items[queue_id]]

    async def settle(self, max_rounds: int = 200, quiet_rounds: int = 5) -> None:
        """Yield until commands, frames and the callbacks they schedule have run.

        Returns after ``quiet_rounds`` event-loop iterations with no command running (commands
        held by :meth:`gate` do not count) and no frame waiting for a client that reads. It never
        sleeps, so code that waits on real timers needs them patched.
        """
        quiet = 0
        for _ in range(max_rounds):
            await asyncio.sleep(0)
            running = sum(not task.done() for task in self._tasks)
            unread = any(
                client.connection.receiving and client.connection.socket.inbox
                for client in self.created_clients
                if client.connection.socket is not None
            )
            quiet = 0 if running > self._gate_waiters or unread else quiet + 1
            if quiet >= quiet_rounds:
                return

    def gate(self, command: str) -> asyncio.Event:
        """Hold every ``command`` (after logging it) until :meth:`release`."""
        return self._gates.setdefault(command, asyncio.Event())

    def release(self, command: str) -> None:
        self._gates.pop(command).set()

    def add_player(
        self,
        player_id: str,
        name: str,
        type: PlayerType = PlayerType.PLAYER,
        available: bool = True,
        volume: int = 30,
        group_members: Iterable[str] = (),
        *,
        enabled: bool = True,
        hide_in_ui: bool = False,
    ) -> None:
        """Register a player and, unless it is a protocol player, its queue (same id)."""
        members = list(group_members)
        if type == PlayerType.GROUP and members:
            volume = self._average_volume(members)
        self.players[player_id] = player = Player(
            player_id=player_id,
            provider="universal_player",
            type=type,
            name=name,
            available=available,
            device_info=DeviceInfo(),
            elapsed_time=0.0,
            elapsed_time_last_updated=time.time(),
            volume_level=volume,
            group_members=UniqueList(members),
            enabled=enabled,
            hide_in_ui=hide_in_ui,
            group_volume=volume,
        )
        self._device_item[player_id] = None
        if type != PlayerType.PROTOCOL:
            self.queues[player_id] = PlayerQueue(
                queue_id=player_id, active=True, display_name=name, available=available, items=0
            )
            self.queue_items[player_id] = []
            self._prev_state[player_id] = None
            self._busy[player_id] = 0
            self._transitioning[player_id] = False
        self._refresh_player(player_id)
        self._player_fingerprints[player_id] = self._fingerprint(player_id)
        self._emit(EventType.PLAYER_ADDED, player_id, player)
        if player_id in self.queues:
            self._emit(EventType.QUEUE_ADDED, player_id, self.queues[player_id])

    def update_player(
        self, player_id: str, *, name: Optional[str] = None, available: Optional[bool] = None
    ) -> None:
        """A player provider renames the player or changes its availability."""
        with self._operation():
            player = self.players[player_id]
            if name is not None:
                player.name = name
            if available is not None:
                player.available = available
            self._touch(player_id)
            queue = self.queues.get(player_id)
            if queue is not None:
                self._flush_players()
                if (queue.display_name, queue.available) != (player.name, player.available):
                    queue.display_name, queue.available = player.name, player.available
                    self._signal(player_id)

    def remove_player(self, player_id: str) -> None:
        """Unregister a player (PLAYER_REMOVED); its queue goes with it."""
        with self._operation():
            del self.players[player_id]
            self.queues.pop(player_id, None)
            self.queue_items.pop(player_id, None)
            self._dirty_players.discard(player_id)
            self._player_fingerprints.pop(player_id, None)
            self._emit(EventType.PLAYER_REMOVED, player_id, None)

    def add_qobuz_provider(self, available: bool = True) -> ProviderInstance:
        """Add (or replace) the Qobuz music provider (PROVIDERS_UPDATED)."""
        provider = ProviderInstance(
            type=ProviderType.MUSIC,
            domain="qobuz",
            name="Qobuz",
            instance_id="qobuz--test",
            supported_features=set(),
            available=available,
            is_streaming_provider=True,
            lookup_key="qobuz--test",
        )
        self.providers = [p for p in self.providers if p.instance_id != provider.instance_id]
        self.providers.append(provider)
        self._emit(EventType.PROVIDERS_UPDATED, None, self.providers)
        return provider

    def set_player_volume(self, player_id: str, level: int) -> None:
        """Volume changed in MA's UI or on the speaker (a group's sets all its members)."""
        with self._operation():
            self._set_volume(player_id, level)

    def device_reports(
        self,
        player_id: str,
        state: Optional[PlaybackState] = None,
        elapsed: Optional[float] = None,
    ) -> None:
        """The speaker changes state or position by itself (not an MA play action)."""
        with self._operation():
            if state is not None:
                self._device_set_state(player_id, state)
            if elapsed is not None:
                player = self.players[player_id]
                player.elapsed_time = elapsed
                player.elapsed_time_last_updated = time.time()
            self._on_player_update(player_id)

    def switch_source(self, player_id: str, source: str = "spotify") -> None:
        """The speaker plays another source: PLAYER_UPDATED, then the queue goes inactive.

        Like MA, a QUEUE_UPDATED only follows when that changes the queue's state, item or
        position (a playing or paused queue); an idle queue goes inactive silently.
        """
        with self._operation():
            player = self.players[player_id]
            player.active_source = source
            player.playback_state = PlaybackState.PLAYING
            player.elapsed_time = 0.0
            player.elapsed_time_last_updated = time.time()
            self._device_item[player_id] = None
            self._touch(player_id)
            self._flush_players()
            self._on_player_update(player_id)

    def start_playing(
        self, queue_id: str, track_ids: Iterable[str], option: QueueOption = QueueOption.REPLACE
    ) -> None:
        """Another client runs ``play_media(queue_id, ["qobuz://track/<id>", ...], option)``."""
        with self._operation():
            self._play_media(queue_id, [f"qobuz://track/{t}" for t in track_ids], option)

    def user_plays_something_else(
        self, queue_id: str, name: str = "Radio", *, track_ids: Optional[Iterable[str]] = None
    ) -> None:
        """Another client replaces the queue, with new item ids, and it plays.

        A radio station called ``name``, or with ``track_ids`` Qobuz tracks (someone played
        Qobuz from MA's own UI).
        """
        if track_ids is not None:
            self.start_playing(queue_id, track_ids)
            return
        radio_id = f"radio-{name}"
        mapping = ProviderMapping(
            item_id=radio_id, provider_domain="tunein", provider_instance="tunein"
        )
        radio = Radio(item_id=radio_id, provider="tunein", name=name, provider_mappings={mapping})
        with self._operation():
            self._play_media(queue_id, [radio], QueueOption.REPLACE)

    def buffer_next(self, queue_id: str) -> None:
        """The speaker starts fetching the next item (``index_in_buffer`` moves to it).

        From then on MA ignores a ``delete_item`` of that item, and REPLACE_NEXT inserts after it.
        """
        with self._operation():
            next_index = self._next_index(queue_id, self.queues[queue_id].current_index)
            assert next_index is not None, "no next item to buffer"
            self._buffer(queue_id, next_index)

    def advance_time(self, queue_id: str, seconds: float) -> None:
        """Let ``seconds`` of playback pass on a playing queue (MA sends no event for that).

        The server's position moves on, and every client's cached time anchor moves back, so
        ``corrected_elapsed_time`` advances as if the wall clock had.
        """
        queue = self._playing_queue(queue_id)
        queue.elapsed_time += seconds
        player = self.players[queue_id]
        player.elapsed_time = (player.elapsed_time or 0.0) + seconds
        for client in self.created_clients:
            cached = client.player_queues.get(queue_id)
            if cached is not None and cached.state == PlaybackState.PLAYING:
                cached.elapsed_time_last_updated -= seconds

    def finish_current_track(self, queue_id: str) -> None:
        """The current track plays to its end on the speaker.

        The speaker moves on to the next item (the buffered one if :meth:`buffer_next` ran) and
        MA follows. At the end of the queue it goes idle, with ``ended`` still False until
        :meth:`mark_ended`.
        """
        with self._operation():
            queue = self._playing_queue(queue_id)
            current, buffered = queue.current_index, queue.index_in_buffer
            items = self.queue_items[queue_id]
            next_index: Optional[int]
            if buffered is not None and current < buffered < len(items):
                next_index = buffered
            else:
                next_index = self._next_index(queue_id, current)
                if next_index is not None and next_index != buffered:
                    self._buffer(queue_id, next_index)
            if next_index is None:
                player = self.players[queue_id]
                player.elapsed_time = float(items[current].duration or 0)
                player.elapsed_time_last_updated = time.time()
                player.playback_state = PlaybackState.IDLE
                self._touch(queue_id)
            else:
                self._device_play(queue_id, items[next_index], 0)
            self._on_player_update(queue_id)

    def mark_ended(self, queue_id: str) -> bool:
        """MA's end-of-queue settle, which runs about 5 s after the last track went idle.

        Like ``playback_tracker._settle_or_resume_delayed``: nothing if the queue is no longer
        idle, the next item if one was added meanwhile, else the queue ends (``ended=True``, the
        last item current at position 0). Returns whether the queue ended.
        """
        with self._operation():
            queue = self.queues[queue_id]
            if queue.state != PlaybackState.IDLE:
                return False
            next_index = self._next_index(queue_id, queue.current_index)
            if next_index is not None:  # "Items added to queue while idle, resuming playback"
                with self._play_action(queue_id):
                    self._play_index(queue_id, next_index)
                return False
            items = self.queue_items[queue_id]
            queue.ended = True
            queue.current_index = len(items) - 1
            queue.current_item = items[-1]
            queue.elapsed_time = 0.0
            queue.elapsed_time_last_updated = time.time()
            queue.index_in_buffer = None
            queue.resume_pos = 0
            self._signal(queue_id)
            return True

    def auto_stop_after_pause(self, queue_id: str) -> None:
        """MA's watchdog stops a queue paused for 30 s: IDLE, same item, resume_pos kept."""
        self.ui_stop(queue_id)

    def ui_pause(self, queue_id: str) -> None:
        """Someone presses pause in MA's UI."""
        with self._operation():
            self._pause(queue_id)

    def ui_resume(self, queue_id: str) -> None:
        """Someone presses play in MA's UI (unpause, or resume)."""
        with self._operation():
            self._play(queue_id)

    def ui_stop(self, queue_id: str) -> None:
        with self._operation():
            self._stop(queue_id)

    def ui_clear(self, queue_id: str) -> None:
        with self._operation():
            self._clear(queue_id)

    def ui_next(self, queue_id: str) -> None:
        with self._operation():
            queue = self.queues[queue_id]
            next_index = self._next_index(queue_id, queue.current_index, is_skip=True)
            assert next_index is not None, "no next item to skip to"
            self._skip_to(queue_id, next_index)

    def ui_previous(self, queue_id: str) -> None:
        """Someone presses previous in MA's UI: back one item if less than 5 s in, else restart."""
        with self._operation():
            queue = self.queues[queue_id]
            index = queue.current_index
            if queue.elapsed_time < 5:  # MA reads the raw, not the extrapolated, position
                index = max(index - 1, 0)
            self._skip_to(queue_id, index)

    def ui_seek(self, queue_id: str, seconds: int) -> None:
        """Someone seeks in MA's UI (the stream restarts there; QUEUE_TIME_UPDATED)."""
        with self._operation():
            self._seek(queue_id, seconds)

    def drop_connections(self, graceful: bool = True) -> None:
        """Close every websocket: ``start_listening`` returns None, or raises ConnectionFailed."""
        for sock in self._open_sockets():
            sock.close(graceful)

    def set_reachable(self, reachable: bool, *, drop: bool = False) -> None:
        """Make new connections fail (or work); ``drop=True`` also breaks the open ones."""
        self.reachable = reachable
        if drop:
            self.drop_connections(graceful=False)

    def restart(self) -> None:
        """MA restarts (add-on update, crash): the music stops, the queues and their ids stay.

        Like MA's ``mass.stop()``: the player providers unload first (clients get PLAYER_UPDATED
        ``available=False`` for every player), then the websockets close. Every queue comes back
        stopped: IDLE, same items and current item, ``index_in_buffer`` None, ``resume_pos``
        where it stopped. Nothing connects until :meth:`come_back`.
        """
        with self._operation():
            for player_id, player in self.players.items():
                player.available = False
                self._touch(player_id)
        self.drop_connections()
        for queue_id, queue in self.queues.items():
            if queue.state in (PlaybackState.PLAYING, PlaybackState.PAUSED):
                queue.elapsed_time = float(queue.corrected_elapsed_time)
                queue.resume_pos = int(queue.elapsed_time)
            queue.state = PlaybackState.IDLE
            queue.index_in_buffer = None
            queue.extra_attributes[ATTR_PLAY_ACTION_IN_PROGRESS] = False
            self._busy[queue_id] = 0
            self._transitioning[queue_id] = False
            self._prev_state[queue_id] = None
            player = self.players[queue_id]
            plays_queue = player.active_source in (queue_id, None)
            if plays_queue and player.playback_state != PlaybackState.IDLE:
                self._device_set_state(queue_id, PlaybackState.IDLE)
        self._dirty_players.clear()
        self.reachable = False

    def come_back(self) -> None:
        """The restarted MA accepts connections again, every player registered again."""
        for player_id, player in self.players.items():
            player.available = True
            if player_id in self.queues:
                self.queues[player_id].available = True
            self._player_fingerprints[player_id] = self._fingerprint(player_id)
        self.reachable = True

    def revoke_token(self, token: str) -> None:
        """Its websockets close at once, and connecting with it fails authentication."""
        self._token_users.pop(token, None)
        for sock in self._open_sockets():
            if sock.token == token:
                sock.close()

    async def login(
        self,
        server_url: str,
        username: str,
        password: str,
        aiohttp_session: Optional[aiohttp.ClientSession] = None,
    ) -> tuple[User, str]:
        """Stands in for ``music_assistant_client.login`` (POST /auth/login)."""
        await asyncio.sleep(0)
        self._check_reachable(server_url)
        if self.setup_required:
            raise CannotConnect(Exception("Login failed with status 403"))
        if self.users.get(username) != password:
            raise LoginFailed("Invalid username or password")
        return self._user(username), self._new_token("session", username)

    def _command_table(self) -> dict[str, Callable[..., Any]]:
        """Handlers by command: they take the command's args by name (``sock``: the caller's)."""
        return {
            # what the client fetches when it connects
            "providers": lambda: self.providers,
            "providers/manifests": lambda: [],
            "players/all": self._cmd_players_all,
            "player_queues/all": lambda: list(self.queues.values()),
            "dashboard/dashboards": lambda: [],
            "dashboard/sessions": lambda: [],
            # what the product sends
            "players/cmd/group_volume": self._cmd_group_volume,
            "player_queues/items": self._cmd_queue_items,
            "player_queues/play_media": self._play_media,
            "player_queues/play_index": self._cmd_play_index,
            "player_queues/play": self._play,
            "player_queues/pause": self._pause,
            "player_queues/stop": self._stop,
            "player_queues/seek": self._seek,
            "player_queues/clear": self._clear,
            "player_queues/delete_item": self._cmd_delete_item,
            "player_queues/repeat": self._cmd_repeat,
            "player_queues/autoplay": self._cmd_autoplay,
            "auth/token/create": self._cmd_token_create,
            "auth/logout": self._cmd_logout,
        }

    def _cmd_players_all(self) -> list[Player]:
        """Every enabled player but the protocol players (those only arrive as events)."""
        players = [
            player
            for player in self.players.values()
            if player.enabled and player.type != PlayerType.PROTOCOL
        ]
        for player in players:
            self._refresh_player(player.player_id)
        return players

    def _cmd_group_volume(self, player_id: str, volume_level: int) -> None:
        self._require_available(player_id)
        self._set_volume(player_id, volume_level)

    def _cmd_queue_items(self, queue_id: str, limit: int = 500, offset: int = 0) -> list[QueueItem]:
        return self.queue_items.get(queue_id, [])[offset : offset + limit]

    def _cmd_play_index(self, queue_id: str, index: str, seek_position: int = 0) -> None:
        self._queue(queue_id)
        with self._play_action(queue_id):
            self._play_index(queue_id, index, seek_position)

    def _cmd_delete_item(self, queue_id: str, item_id_or_index: str) -> None:
        queue = self._queue(queue_id)
        index = self._index_by_id(queue_id, item_id_or_index)
        if index is None:
            raise InvalidDataError(f"Item {item_id_or_index} not found in queue")
        owned = max(queue.current_index or 0, queue.index_in_buffer or 0)
        if queue.index_in_buffer is not None and index <= owned:
            return  # MA ignores a "delete requested for item already loaded in buffer"
        items = self.queue_items[queue_id]
        self._update_items(queue_id, items[:index] + items[index + 1 :])

    def _cmd_repeat(self, queue_id: str, repeat_mode: str) -> None:
        queue = self.queues[queue_id]
        mode = RepeatMode(repeat_mode)
        if queue.repeat_mode == mode:
            return
        if queue.is_dynamic:
            raise InvalidCommand("Cannot change repeat while the queue is in dynamic mode")
        queue.repeat_mode = mode
        self._signal(queue_id)

    def _cmd_autoplay(self, queue_id: str, autoplay_enabled: bool) -> None:
        self.queues[queue_id].autoplay_enabled = autoplay_enabled
        self._signal(queue_id)

    def _cmd_token_create(self, sock: _ServerSocket) -> str:
        if sock.username in self.guest_users:
            raise InsufficientPermissions("Guest users cannot create long-lived tokens")
        return self._new_token("long-lived", sock.username)

    def _cmd_logout(self, sock: _ServerSocket) -> None:
        asyncio.get_running_loop().call_soon(self.revoke_token, sock.token)  # after the answer

    def _accept(self, url: str) -> _ServerSocket:
        """A client connects: it gets the server info first."""
        self._check_reachable(url)
        sock = _ServerSocket()
        sock.push(self._server_info())
        if self.setup_required:
            sock.push({"message_id": "connection", "error_code": 503, "details": "Setup required"})
            sock.close()
        return sock

    def _check_reachable(self, url: str) -> None:
        """Raise what the client raises when nothing answers at ``url``."""
        address = urlparse(url)
        known = {urlparse(known_url).netloc for known_url in (self.url, *self.aliases)}
        if not self.reachable or address.netloc not in known:
            err = aiohttp.ClientConnectionError(
                f"Cannot connect to host {address.hostname}:{address.port} ssl:default "
                "[Connect call failed]"
            )
            raise CannotConnect(err) from err

    def _server_info(self) -> dict[str, Any]:
        info = ServerInfoMessage(
            server_id="fakemaserver",
            server_version=self.server_version,
            schema_version=SCHEMA_VERSION,
            min_supported_schema_version=self.min_schema_version,
            base_url=self.url,
            homeassistant_addon=True,
            onboard_done=not self.setup_required,
            name="Music Assistant",
            status=CoreState.RUNNING,
        )
        result: dict[str, Any] = _wire(info.to_dict())
        return result

    def _open_sockets(self) -> list[_ServerSocket]:
        return [sock for client in self.clients if (sock := client.connection.socket) is not None]

    def _receive(self, sock: _ServerSocket, frame: dict[str, Any]) -> None:
        """A frame from a client: MA handles each command in a task of its own."""
        task = asyncio.get_running_loop().create_task(self._run_command(sock, frame))
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)

    async def _run_command(self, sock: _ServerSocket, frame: dict[str, Any]) -> None:
        message_id, command = frame["message_id"], frame["command"]
        args: dict[str, Any] = frame.get("args") or {}
        await asyncio.sleep(0)
        if command == "auth":
            sock.push(self._auth(sock, message_id, args.get("token", "")))
            return
        self.command_log.append((command, args))
        gate = self._gates.get(command)
        if gate is not None:
            self._gate_waiters += 1
            try:
                await gate.wait()
            finally:
                self._gate_waiters -= 1
        failure = self.fail_next.pop(command, None)
        if failure == DROP:
            sock.close()
        elif failure == HANG:
            pass
        elif failure is not None:
            sock.push(_error_frame(message_id, failure))
        else:
            sock.push(self._answer(sock, message_id, command, args))
            self._flush_players()  # after the answer, like MA's 0.25 s debounce

    def _answer(
        self, sock: _ServerSocket, message_id: str, command: str, args: dict[str, Any]
    ) -> dict[str, Any]:
        """Run ``command`` and build its result or error frame, like MA's websocket handler.

        Like MA's ``parse_arguments(strict=False)``, args the handler does not take are ignored.
        """
        handler = self._commands.get(command)
        if handler is None:
            return _error_frame(message_id, InvalidCommand(f"Invalid command: {command}"))
        params = inspect.signature(handler).parameters
        kwargs = {name: args[name] for name in params if args.get(name) is not None}
        if "sock" in params:
            kwargs["sock"] = sock
        try:
            with self._operation(flush=False):
                result = handler(**kwargs)
        except Exception as err:  # MA answers every error
            return _error_frame(message_id, err)
        return _success_frame(message_id, result)

    def _auth(self, sock: _ServerSocket, message_id: str, token: str) -> dict[str, Any]:
        username = self._token_users.get(token)
        if username is None:
            return _error_frame(message_id, InvalidToken("Invalid or expired token"))
        sock.token, sock.username = token, username
        user = self._user(username).to_dict()
        return _success_frame(message_id, {"authenticated": True, "user": user})

    def _new_token(self, kind: str, username: str) -> str:
        self._token_seq += 1
        token = f"fake-{kind}-token-{self._token_seq}"
        self._token_users[token] = username
        return token

    def _user(self, username: str) -> User:
        role = "guest" if username in self.guest_users else "admin"
        return User(user_id=f"user-{username}", username=username, role=role)

    @contextmanager
    def _operation(self, flush: bool = True) -> Iterator[None]:
        """Group state changes: PLAYER_UPDATED is coalesced and emitted when the group ends."""
        self._op_depth += 1
        try:
            yield
        finally:
            self._op_depth -= 1
            if self._op_depth == 0 and flush:
                self._flush_players()

    def _emit(self, event: EventType, object_id: Optional[str], data: Any) -> None:
        message = MassEvent(event=event, object_id=object_id, data=data).to_dict()
        for sock in self._open_sockets():
            if sock.token:  # MA sends events only after the auth command
                sock.push(_wire(message))

    def _touch(self, player_id: str) -> None:
        """Mark the player for a PLAYER_UPDATED when the operation ends."""
        self._dirty_players.add(player_id)

    def _flush_players(self) -> None:
        """PLAYER_UPDATED for each touched player that really changed."""
        dirty, self._dirty_players = sorted(self._dirty_players), set()
        for player_id in dirty:
            self._refresh_player(player_id)
            fingerprint = self._fingerprint(player_id)
            if fingerprint != self._player_fingerprints.get(player_id):
                self._player_fingerprints[player_id] = fingerprint
                self._emit(EventType.PLAYER_UPDATED, player_id, self.players[player_id])

    def _fingerprint(self, player_id: str) -> dict[str, Any]:
        data: dict[str, Any] = self.players[player_id].to_dict()
        # like MA's player fingerprint: a position change alone is not an update
        del data["elapsed_time"], data["elapsed_time_last_updated"]
        return data

    def _refresh_player(self, player_id: str) -> None:
        """``current_media`` like MA: the queue's current item while the queue is active."""
        player = self.players[player_id]
        queue = self.queues.get(player_id)
        item = None
        if queue is not None and player.active_source in (player_id, None):
            item = queue.current_item
        player.current_media = None
        if item is not None:
            player.current_media = PlayerMedia(
                uri=item.uri,
                media_type=item.media_type,
                title=item.name,
                duration=item.duration,
                source_id=player_id,
                queue_item_id=item.queue_item_id,
            )

    def _signal(self, queue_id: str, items_changed: bool = False) -> None:
        """MA's ``signal_update``: QUEUE_ITEMS_UPDATED (if the items changed), QUEUE_UPDATED."""
        queue = self.queues[queue_id]
        if items_changed:
            self._emit(EventType.QUEUE_ITEMS_UPDATED, queue_id, queue)
        self._emit(EventType.QUEUE_UPDATED, queue_id, queue)
        self._touch(queue_id)

    def _queue(self, queue_id: str) -> PlayerQueue:
        queue = self.queues.get(queue_id)
        if queue is None:
            raise PlayerUnavailableError(f"Queue {queue_id} is not available")
        return queue

    def _require_available(self, player_id: str) -> None:
        player = self.players.get(player_id)
        if player is None or not player.available or not player.enabled:
            raise PlayerUnavailableError(f"Player {player_id} is not available")

    def _playing_queue(self, queue_id: str) -> PlayerQueue:
        queue = self.queues[queue_id]
        if queue.state != PlaybackState.PLAYING:
            raise RuntimeError(f"queue {queue_id} is not playing")
        return queue

    @contextmanager
    def _play_action(self, queue_id: str) -> Iterator[None]:
        """MA's ``handle_play_action``: the queue is flagged busy while a play action runs."""
        queue = self.queues[queue_id]
        first = self._busy[queue_id] == 0
        self._busy[queue_id] += 1
        queue.extra_attributes[ATTR_PLAY_ACTION_IN_PROGRESS] = True
        if first:
            self._signal(queue_id)
        try:
            yield
        finally:
            self._busy[queue_id] -= 1
            if self._busy[queue_id] == 0:
                queue.extra_attributes[ATTR_PLAY_ACTION_IN_PROGRESS] = False
                self._on_player_update(queue_id)
                self._signal(queue_id)

    def _on_player_update(self, queue_id: str) -> None:
        """MA's ``on_player_update`` for the player's queue."""
        queue = self.queues[queue_id]
        queue.active = self.players[queue_id].active_source in (queue_id, None)
        if not queue.active and self._prev_state[queue_id] is None:
            queue.state = PlaybackState.IDLE  # silently: nothing tracked to compare with
            return
        if not self._transitioning[queue_id]:
            self._update_queue_from_player(queue_id)

    def _update_queue_from_player(self, queue_id: str) -> None:
        """``playback_tracker._update_queue_from_player`` (no flow mode)."""
        player = self.players[queue_id]
        queue = self.queues[queue_id]
        queue.display_name = player.name
        queue.available = player.available
        queue.state = player.playback_state if queue.active else PlaybackState.IDLE
        if queue.active and queue.state in (PlaybackState.PLAYING, PlaybackState.PAUSED):
            index = self._index_by_id(queue_id, self._device_item[queue_id])
            if index is None:
                return  # the speaker is still switching, or plays an item MA no longer has
            queue.current_index = index
            queue.current_item = self.queue_items[queue_id][index]
            queue.elapsed_time = float(player.corrected_elapsed_time or 0.0)
            queue.elapsed_time_last_updated = time.time()
        prev = self._prev_state[queue_id] or (PlaybackState.IDLE, None, 0)
        item_id = queue.current_item.queue_item_id if queue.current_item else None
        new = (queue.state, item_id, int(queue.elapsed_time))
        self._prev_state[queue_id] = new if queue.active else None
        if new == prev:
            return
        if new[:2] == prev[:2]:  # only the position moved
            if abs(new[2] - prev[2]) > 2:
                self._emit(EventType.QUEUE_TIME_UPDATED, queue_id, queue.elapsed_time)
                self._touch(queue_id)
            return
        self._signal(queue_id)

    def _play_media(
        self, queue_id: str, media: Union[Any, list[Any]], option: Union[QueueOption, str]
    ) -> None:
        """MA's ``play_media``: items it cannot resolve or play are left out."""
        self._queue(queue_id)
        with self._play_action(queue_id):
            self._transitioning[queue_id] = False
            self._require_available(queue_id)
            new_items = []
            for entry in media if isinstance(media, list) else [media]:
                try:
                    media_item = self._resolve_uri(entry) if isinstance(entry, str) else entry
                except ProviderUnavailableError:
                    continue
                if media_item.available:
                    new_items.append(self._new_queue_item(queue_id, media_item))
            if not new_items:
                raise MediaNotFoundError("No playable items found")
            self._enqueue(queue_id, new_items, QueueOption(option))

    def _resolve_uri(self, uri: str) -> Track:
        """A ``qobuz://track/<id>`` URI, as long as an available provider can resolve it."""
        domain, path = uri.split("://", 1)
        track_id = path.split("/", 1)[1]
        provider = next((p for p in self.providers if p.domain == domain and p.available), None)
        if provider is None:
            raise ProviderUnavailableError(f"Provider {domain} is not available")
        mapping = ProviderMapping(
            item_id=track_id,
            provider_domain=domain,
            provider_instance=provider.instance_id,
            available=track_id not in self.unplayable_track_ids,
        )
        item_id, provider_key = track_id, provider.instance_id
        if track_id in self.library_track_ids:
            item_id = self._library_ids.setdefault(track_id, str(len(self._library_ids) + 1))
            provider_key = "library"
        artist = ItemMapping(
            item_id=f"artist-{track_id}",
            provider=provider_key,
            name="Test Artist",
            media_type=MediaType.ARTIST,
        )
        return Track(
            item_id=item_id,
            provider=provider_key,
            name=f"Track {track_id}",
            duration=TRACK_DURATION,
            provider_mappings={mapping},
            artists=UniqueList([artist]),
        )

    def _new_queue_item(self, queue_id: str, media_item: MediaItem) -> QueueItem:
        self._item_seq += 1
        item = QueueItem.from_media_item(queue_id, media_item)
        item.queue_item_id = f"qi{self._item_seq:04d}"
        return item

    def _enqueue(self, queue_id: str, new_items: list[QueueItem], option: QueueOption) -> None:
        """``queue_loader._enqueue_with_option`` for the options the product and tests use."""
        queue = self.queues[queue_id]
        if queue.ended and option == QueueOption.REPLACE_NEXT:
            self._clear(queue_id, skip_stop=True)  # an ended queue starts afresh unless added to
        items = self.queue_items[queue_id]
        if option == QueueOption.REPLACE:
            queue.index_in_buffer = None
            queue.ended = False
            queue.is_dynamic = False
            self._update_items(queue_id, new_items)
            self._play_index(queue_id, 0)
        elif option == QueueOption.REPLACE_NEXT:
            self._update_items(queue_id, items[: self._insert_index(queue)] + new_items)
            self._ensure_current_index(queue_id)
        elif option == QueueOption.ADD:
            self._update_items(queue_id, items + new_items)
            self._ensure_current_index(queue_id)
        else:
            raise NotImplementedError(f"the fake does not do option {option}")

    def _insert_index(self, queue: PlayerQueue) -> int:
        """Where REPLACE_NEXT inserts: after what the player already owns while it plays."""
        current = queue.current_index or 0
        if queue.state in (PlaybackState.PLAYING, PlaybackState.PAUSED):
            return max(current, queue.index_in_buffer or 0) + 1
        return current + 1

    def _update_items(self, queue_id: str, items: list[QueueItem]) -> None:
        self.queue_items[queue_id] = items
        self.queues[queue_id].items = len(items)
        self._signal(queue_id, items_changed=True)

    def _ensure_current_index(self, queue_id: str) -> None:
        queue = self.queues[queue_id]
        if queue.current_index is None:
            queue.current_index = 0
            queue.current_item = self.queue_items[queue_id][0]
            self._signal(queue_id)

    def _play_index(self, queue_id: str, index: Union[int, str], seek_position: float = 0) -> None:
        """Body of MA's ``play_index`` (run it inside :meth:`_play_action`)."""
        queue = self.queues[queue_id]
        queue.resume_pos = 0
        if isinstance(index, str):
            found = self._index_by_id(queue_id, index)
            if found is None:
                raise InvalidDataError(f"Item {index} not found in queue")
            index = found
        queue.index_in_buffer = index
        self._require_available(queue_id)
        item = self.queue_items[queue_id][index]
        queue.current_index = index
        queue.current_item = item
        queue.ended = False
        queue.elapsed_time = float(seek_position)
        queue.elapsed_time_last_updated = time.time()
        self._device_play(queue_id, item, seek_position)
        self._signal(queue_id)
        self._transitioning[queue_id] = False

    def _play(self, queue_id: str) -> None:
        """MA's ``play``: unpause a paused queue, else resume it."""
        queue = self._queue(queue_id)
        with self._play_action(queue_id):
            self._require_available(queue_id)
            if queue.active and queue.state == PlaybackState.PAUSED:
                self._device_set_state(queue_id, PlaybackState.PLAYING)
            else:
                self._resume(queue_id)

    def _resume(self, queue_id: str) -> None:
        """MA's ``resume``: the current item where it stopped; an ended queue starts over."""
        queue = self.queues[queue_id]
        items = self.queue_items[queue_id]
        item, position = queue.current_item, queue.resume_pos or queue.elapsed_time
        if queue.ended and items:
            item, position = items[0], 0
        if item is None:
            raise QueueEmpty(f"Resume queue requested but queue {queue.display_name} is empty")
        self._play_index(queue_id, item.queue_item_id, int(position))

    def _pause(self, queue_id: str) -> None:
        queue = self.queues.get(queue_id)
        if queue is None:
            return
        self._transitioning[queue_id] = False
        if queue.active and queue.state == PlaybackState.PLAYING:
            queue.resume_pos = int(queue.corrected_elapsed_time)
        if self.players[queue_id].playback_state == PlaybackState.PLAYING:
            self._device_set_state(queue_id, PlaybackState.PAUSED)
        self._on_player_update(queue_id)

    def _stop(self, queue_id: str) -> None:
        queue = self._queue(queue_id)
        with self._play_action(queue_id):
            self._transitioning[queue_id] = False
            if queue.active and queue.state == PlaybackState.PLAYING:
                queue.resume_pos = int(queue.corrected_elapsed_time)
            if self.players[queue_id].playback_state != PlaybackState.IDLE:
                self._device_set_state(queue_id, PlaybackState.IDLE)

    def _skip_to(self, queue_id: str, index: int) -> None:
        """MA's next/previous: shows the item at once, then plays it (MA waits 1 s for that)."""
        queue = self.queues[queue_id]
        with self._play_action(queue_id):
            self._transitioning[queue_id] = True
            queue.current_index = index
            queue.current_item = self.queue_items[queue_id][index]
            queue.elapsed_time = 0.0
            queue.elapsed_time_last_updated = time.time()
            self._signal(queue_id)
        with self._play_action(queue_id):
            self._play_index(queue_id, index)

    def _seek(self, queue_id: str, position: int) -> None:
        """MA's ``seek``: the stream starts again at ``position``."""
        queue = self.queues.get(queue_id)
        if queue is None or not queue.active:
            raise InvalidCommand(f"Queue {queue_id} is not active")
        self._require_available(queue_id)
        position = max(0, int(position))
        queue.elapsed_time = float(position)
        queue.elapsed_time_last_updated = time.time()
        self._signal(queue_id)
        with self._play_action(queue_id):
            self._play_index(queue_id, queue.current_index, seek_position=position)

    def _clear(self, queue_id: str, skip_stop: bool = False) -> None:
        queue = self._queue(queue_id)
        queue.is_dynamic = False
        queue.ended = False
        needs_stop = queue.state != PlaybackState.IDLE and not skip_stop
        queue.current_index = None
        queue.current_item = None
        queue.elapsed_time = 0.0
        queue.elapsed_time_last_updated = time.time()
        queue.index_in_buffer = None
        self._update_items(queue_id, [])
        if needs_stop:
            self._stop(queue_id)  # MA runs this stop in a task: after the clear's own events

    def _buffer(self, queue_id: str, index: int) -> None:
        """The speaker loaded item ``index`` into its buffer."""
        self.queues[queue_id].index_in_buffer = index
        self._signal(queue_id)

    def _next_index(self, queue_id: str, index: int, is_skip: bool = False) -> Optional[int]:
        """MA's ``_get_next_index``, repeat modes included."""
        queue = self.queues[queue_id]
        if queue.repeat_mode == RepeatMode.ONE and not is_skip:
            return index
        if index + 1 < len(self.queue_items[queue_id]):
            return index + 1
        return 0 if queue.repeat_mode == RepeatMode.ALL else None

    def _index_by_id(self, queue_id: str, item_id: Optional[str]) -> Optional[int]:
        items = self.queue_items[queue_id]
        return next((i for i, item in enumerate(items) if item.queue_item_id == item_id), None)

    def _device_play(self, player_id: str, item: QueueItem, seek_position: float) -> None:
        """The speaker starts ``item`` (a dead speaker stays idle)."""
        player = self.players[player_id]
        dead = player_id in self.dead_player_ids
        player.playback_state = PlaybackState.IDLE if dead else PlaybackState.PLAYING
        player.active_source = player_id
        player.elapsed_time = float(seek_position)
        player.elapsed_time_last_updated = time.time()
        self._device_item[player_id] = item.queue_item_id
        self._touch(player_id)

    def _device_set_state(self, player_id: str, state: PlaybackState) -> None:
        player = self.players[player_id]
        if player.playback_state == PlaybackState.PLAYING:
            player.elapsed_time = float(player.corrected_elapsed_time or 0.0)
        player.elapsed_time_last_updated = time.time()
        player.playback_state = state
        self._touch(player_id)

    def _set_volume(self, player_id: str, level: int) -> None:
        """A group's level goes to all its members; a member's moves its groups' average."""
        player = self.players[player_id]
        level = max(0, min(100, int(level)))
        changed = [player_id]
        if player.type == PlayerType.GROUP:
            changed += player.group_members
        for changed_id in changed:
            self.players[changed_id].volume_level = level
            self.players[changed_id].group_volume = level
            self._touch(changed_id)
        for group in self.players.values():
            if player_id in group.group_members:
                group.group_volume = self._average_volume(group.group_members)
                self._touch(group.player_id)

    def _average_volume(self, player_ids: list[str]) -> int:
        levels = [self.players[player_id].volume_level or 0 for player_id in player_ids]
        return round(sum(levels) / len(levels))


class FakeMusicAssistantClient(MusicAssistantClient):
    """The real ``MusicAssistantClient`` with its websocket going to ``server``.

    Build it through ``server.client_class`` (bound to that server, with the real signature) or
    ``server.client_factory``.
    """

    server: ClassVar[FakeMusicAssistantServer]
    connection: FakeWebsocketsConnection

    def __init__(
        self,
        server_url: str,
        aiohttp_session: Optional[aiohttp.ClientSession],
        token: Optional[str] = None,
        ssl_context: Any = None,
        locale: Optional[str] = None,
    ) -> None:
        super().__init__(server_url, aiohttp_session, token, ssl_context, locale)
        self.connection = FakeWebsocketsConnection(self.server, server_url, token)
        self.server.created_clients.append(self)


class FakeWebsocketsConnection:
    """Stands in for ``music_assistant_client``'s ``WebsocketsConnection``.

    The same methods, raising the same exceptions, but the frames go to the fake server.
    """

    def __init__(
        self, server: FakeMusicAssistantServer, server_url: str, auth_token: Optional[str]
    ) -> None:
        self.ws_server_url = get_websocket_url(server_url)
        self.auth_token = auth_token
        self.socket: Optional[_ServerSocket] = None
        self.receiving = False
        self._server = server
        self._seen_close = False

    @property
    def connected(self) -> bool:
        """Connected until the client has read the close frame."""
        return self.socket is not None and not self._seen_close

    async def connect(self) -> dict[str, Any]:
        """Connect and return the first frame (the server info)."""
        await asyncio.sleep(0)
        self.socket = self._server._accept(self.ws_server_url)
        return await self.receive_message()

    async def disconnect(self) -> None:
        if self.socket is not None:
            self.socket.close()  # wakes a pending receive, like aiohttp's CLOSING message
        self.socket = None

    async def receive_message(self) -> dict[str, Any]:
        assert self.socket is not None
        self.receiving = True
        try:
            frame = await self.socket.next_frame()
        finally:
            self.receiving = False
        if frame is _CLOSE:
            self._seen_close = True
            raise ConnectionClosed("Connection was closed.")
        if frame is _ERROR:
            self._seen_close = True
            raise ConnectionFailed
        result: dict[str, Any] = frame
        return result

    async def send_message(self, message: dict[str, Any]) -> None:
        if not self.connected:
            raise NotConnected
        assert self.socket is not None
        if self.socket.closed:  # closed by the server, the close frame not read yet
            raise aiohttp.ClientConnectionResetError("Cannot write to closing transport")
        self._server._receive(self.socket, _wire(message))


class _ServerSocket:
    """The server's end of one websocket: the frames waiting for the client, and its login."""

    def __init__(self) -> None:
        self.token = ""  # both set by the auth command
        self.username = ""
        self.closed = False
        self.inbox: deque[Any] = deque()
        self._waiter: Optional[asyncio.Future[None]] = None

    def push(self, frame: Any) -> None:
        if self.closed:
            return
        self.inbox.append(frame)
        if self._waiter is not None and not self._waiter.done():
            self._waiter.set_result(None)

    def close(self, graceful: bool = True) -> None:
        """Close from the server side: the client reads a close frame, or a websocket error."""
        self.push(_CLOSE if graceful else _ERROR)
        self.closed = True

    async def next_frame(self) -> Any:
        """The next frame; once closed, the close frame over and over."""
        while not self.inbox:
            self._waiter = asyncio.get_running_loop().create_future()
            try:
                await self._waiter
            finally:
                self._waiter = None
        frame = self.inbox[0]
        if frame is _CLOSE or frame is _ERROR:
            return frame
        return self.inbox.popleft()


def _wire(value: Any) -> Any:
    """``value`` as it arrives on the other side of the websocket."""
    return json_loads(json_dumps(value))


def _success_frame(message_id: str, result: Any) -> dict[str, Any]:
    frame: dict[str, Any] = _wire(SuccessResultMessage(message_id, result).to_dict())
    return frame


def _error_frame(message_id: str, err: BaseException) -> dict[str, Any]:
    """What MA sends for an exception (the client maps ``error_code`` back to it)."""
    code = getattr(err, "error_code", 999)
    return {"message_id": message_id, "error_code": code, "details": str(err) or type(err).__name__}


def _item_track_id(item: QueueItem) -> str:
    """Provider track id of a queue item (a library item's is in its provider mapping)."""
    media_item = item.media_item
    assert media_item is not None
    if media_item.provider == "library":
        return str(next(iter(media_item.provider_mappings)).item_id)
    return str(media_item.item_id)
