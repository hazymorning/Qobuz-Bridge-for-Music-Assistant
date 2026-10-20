"""In-memory Music Assistant 2.10.7 server that drives the real ``music-assistant-client``.

:class:`FakeMusicAssistantServer` keeps what a real MA server keeps (players, queues, queue items,
providers, tokens) and outlives the single-use clients the product builds on every reconnect.
Its queue logic is a port of MA 2.10.7 ``controllers/player_queues`` and it emits the same
events in the same order. Only the websocket is fake: :class:`FakeMusicAssistantClient` is the
real client with :class:`FakeWebsocketsConnection` swapped in, so the product's error handling
and event races run against real client code.

Time never passes on its own: :meth:`FakeMusicAssistantServer.advance_time`, ``finish_current_track``,
``mark_ended`` and ``auto_stop_after_pause`` stand in for MA's timers, and ``settle()`` only
yields to the event loop, so tests patch the product's timing constants.
"""

from __future__ import annotations

import asyncio
import copy
import inspect
import logging
import random
import time
from collections import deque
from collections.abc import Iterable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from typing import Any, Callable, ClassVar, Optional, Union
from urllib.parse import urlparse

import aiohttp
from music_assistant_client import MusicAssistantClient
from music_assistant_client.connection import get_websocket_url
from music_assistant_client.exceptions import (
    CannotConnect,
    ConnectionClosed,
    ConnectionFailed,
    InvalidState,
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
    PlayerFeature,
    PlayerType,
    ProviderType,
    QueueOption,
    RepeatMode,
)
from music_assistant_models.errors import (
    AuthenticationRequired,
    InsufficientPermissions,
    InvalidCommand,
    InvalidDataError,
    InvalidProviderURI,
    InvalidToken,
    LoginFailed,
    MediaNotFoundError,
    MusicAssistantError,
    PlayerUnavailableError,
    ProviderUnavailableError,
    QueueEmpty,
    UnsupportedFeaturedException,
)
from music_assistant_models.event import MassEvent
from music_assistant_models.media_items import (
    ItemMapping,
    MediaItem,
    ProviderMapping,
    Radio,
    Track,
    media_from_dict,
)
from music_assistant_models.player import DeviceInfo, Player, PlayerMedia
from music_assistant_models.player_queue import PlayerQueue
from music_assistant_models.provider import ProviderInstance
from music_assistant_models.queue_item import QueueItem
from music_assistant_models.unique_list import UniqueList


logger = logging.getLogger(__name__)

DEFAULT_URL = "http://127.0.0.1:8095"
DEFAULT_TOKEN = "fake-ma-long-lived-token"
QOBUZ_INSTANCE_ID = "qobuz--test"
SERVER_VERSION = "2.10.7"
SCHEMA_VERSION = 65  # MA 2.10.7 constants.API_SCHEMA_VERSION
MIN_SCHEMA_VERSION = 28  # MA 2.10.7 constants.MIN_SCHEMA_VERSION
ATTR_PLAY_ACTION_IN_PROGRESS = "play_action_in_progress"
DEFAULT_TRACK_DURATION = 200  # seconds
VOLUME_STEP = 5
# Provider domains whose URIs only resolve while a matching provider instance is set up. Any
# other scheme (e.g. ``test://track/1``) resolves leniently with provider instance == scheme.
STRICT_PROVIDER_DOMAINS = frozenset({"qobuz"})
DEFAULT_PLAYER_FEATURES = frozenset(
    {
        PlayerFeature.POWER,
        PlayerFeature.VOLUME_SET,
        PlayerFeature.VOLUME_MUTE,
        PlayerFeature.PAUSE,
        PlayerFeature.SEEK,
        PlayerFeature.NEXT_PREVIOUS,
        PlayerFeature.ENQUEUE,
        PlayerFeature.GAPLESS_PLAYBACK,
        PlayerFeature.PLAY_MEDIA,
    }
)
# Commands that only read state. ``FakeMusicAssistantServer.writes`` filters these out of ``command_log``.
READ_COMMANDS = frozenset(
    {
        "providers",
        "providers/manifests",
        "players/all",
        "players/get",
        "player_queues/all",
        "player_queues/get",
        "player_queues/items",
        "player_queues/get_active_queue",
        "dashboard/dashboards",
        "dashboard/sessions",
        "music/item_by_uri",
        "music/item",
        "music/tracks/get",
        "auth/me",
        "auth/tokens",
    }
)
# Special ``fail_next`` values (anything else must be an exception instance).
DROP = "drop"  # close the socket gracefully instead of answering (start_listening returns None)
DROP_HARD = "drop_hard"  # websocket error instead of answering (start_listening raises)
HANG = "hang"  # never answer

_CLOSE = object()  # graceful close frame (ConnectionClosed on the client)
_ERROR = object()  # websocket error frame (ConnectionFailed on the client)
_SESSION_PLACEHOLDER = object()  # keeps the real WebsocketsConnection from creating a session


def committed_index(queue: PlayerQueue) -> Optional[int]:
    """Return the highest index the player owns (MA 2.10.7 ``helpers.committed_index``)."""
    if queue.current_index is None:
        return queue.index_in_buffer
    if queue.index_in_buffer is None:
        return queue.current_index
    return max(queue.current_index, queue.index_in_buffer)


def _wire(value: Any) -> Any:
    """Return ``value`` exactly as it arrives on the other side of the websocket."""
    return json_loads(json_dumps(value))


@dataclass
class _TokenInfo:
    token_id: str
    username: str
    name: str
    long_lived: bool


class _ServerSocket:
    """Server side of one websocket connection: the queue of frames waiting for the client."""

    def __init__(
        self, server: FakeMusicAssistantServer, connection: FakeWebsocketsConnection
    ) -> None:
        self.server = server
        self.connection = connection
        self.authenticated = False
        self.token: Optional[str] = None
        self.username: Optional[str] = None
        self.closed = False
        self.inbox: deque[Any] = deque()
        self._waiter: Optional[asyncio.Future[None]] = None

    def push(self, message: Any) -> None:
        if self.closed:
            return
        self.inbox.append(message)
        self._wake()

    def close(self, graceful: bool = True) -> None:
        """Close from the server side (restart, revoked token, network error...)."""
        if self.closed:
            return
        self.inbox.append(_CLOSE if graceful else _ERROR)
        self.closed = True
        self._wake()
        self.server._forget_socket(self)

    def close_by_client(self) -> None:
        if not self.closed:
            # wakes a reader blocked in receive(), like aiohttp's CLOSING message
            self.inbox.append(_CLOSE)
            self.closed = True
            self._wake()
        self.server._forget_socket(self)

    def from_client(self, raw: dict[str, Any]) -> None:
        if self.closed:
            return  # written to a closing socket: the server never reads it
        self.server._schedule_command(self, raw)

    async def next_message(self) -> Any:
        while not self.inbox:
            self._waiter = asyncio.get_running_loop().create_future()
            try:
                await self._waiter
            finally:
                self._waiter = None
        message = self.inbox[0]
        if message is _CLOSE or message is _ERROR:
            return message  # a closed socket keeps reporting closed
        return self.inbox.popleft()

    def _wake(self) -> None:
        if self._waiter is not None and not self._waiter.done():
            self._waiter.set_result(None)


class FakeWebsocketsConnection:
    """Drop-in for ``music_assistant_client.connection.WebsocketsConnection``.

    Same public surface (``ws_server_url``, ``auth_token``, ``ssl_context``, ``connected``,
    ``connect``, ``disconnect``, ``receive_message``, ``send_message``) and the same exceptions,
    but the frames go to a :class:`FakeMusicAssistantServer` instead of aiohttp.
    """

    def __init__(
        self,
        server: FakeMusicAssistantServer,
        server_url: str,
        aiohttp_session: Optional[aiohttp.ClientSession],
        auth_token: Optional[str] = None,
        ssl_context: Any = None,
    ) -> None:
        self.ws_server_url = get_websocket_url(server_url)
        self.auth_token = auth_token
        self.ssl_context = ssl_context
        self.client: Optional[FakeMusicAssistantClient] = None
        self._server = server
        self._aiohttp_session = aiohttp_session
        self._aiohttp_session_provided = aiohttp_session is not None
        self._socket: Optional[_ServerSocket] = None
        self._seen_close = False
        self._receiving = False

    @property
    def connected(self) -> bool:
        """Return if we're currently connected (closed once the client read the close frame)."""
        return self._socket is not None and not self._seen_close

    async def connect(self) -> dict[str, Any]:
        """Connect and return the first message (server info), like the real connection."""
        if self._socket is not None:
            raise InvalidState("Already connected")
        await asyncio.sleep(0)
        self._socket = self._server._accept(self)
        self._seen_close = False
        return await self.receive_message()

    async def disconnect(self) -> None:
        """Disconnect the client."""
        if self._socket is not None:
            self._socket.close_by_client()
        self._socket = None

    async def receive_message(self) -> dict[str, Any]:
        """Receive the next message from the server (or raise like the real connection)."""
        assert self._socket
        sock = self._socket
        if self._receiving:
            # aiohttp refuses two concurrent receive() calls on one websocket
            raise RuntimeError("Concurrent call to receive() is not allowed")
        self._receiving = True
        try:
            message = await sock.next_message()
        finally:
            self._receiving = False
        if message is _CLOSE:
            self._seen_close = True
            raise ConnectionClosed("Connection was closed.")
        if message is _ERROR:
            self._seen_close = True
            raise ConnectionFailed
        assert isinstance(message, dict)
        return message

    async def send_message(self, message: dict[str, Any]) -> None:
        """Send a message to the server (NotConnected if not connected)."""
        if not self.connected:
            raise NotConnected
        assert self._socket
        if self._socket.closed:
            raise aiohttp.ClientConnectionResetError("Cannot write to closing transport")
        self._socket.from_client(_wire(message))


class FakeMusicAssistantClient(MusicAssistantClient):
    """The real ``MusicAssistantClient`` wired to a :class:`FakeMusicAssistantServer`.

    Do not instantiate this class directly: use ``server.client_class`` (a subclass bound to the
    server) or ``server.client_factory(url, session, token)``. The constructor signature is the
    real one, so the bound class can also replace ``MusicAssistantClient`` via monkeypatch.
    """

    _fake_server: ClassVar[Optional[FakeMusicAssistantServer]] = None

    def __init__(
        self,
        server_url: str,
        aiohttp_session: Optional[aiohttp.ClientSession],
        token: Optional[str] = None,
        ssl_context: Any = None,
        locale: Optional[str] = None,
    ) -> None:
        server = type(self)._fake_server
        if server is None:
            raise TypeError(
                "Create fake clients through FakeMusicAssistantServer.client_class or "
                "FakeMusicAssistantServer.client_factory so they are bound to a server"
            )
        session_arg = aiohttp_session if aiohttp_session is not None else _SESSION_PLACEHOLDER
        super().__init__(server_url, session_arg, token, ssl_context, locale)  # type: ignore[arg-type]
        connection = FakeWebsocketsConnection(
            server, server_url, aiohttp_session, token, ssl_context
        )
        connection.client = self
        self.connection = connection  # type: ignore[assignment]
        self.fake_server = server
        server.created_clients.append(self)


class FakeMusicAssistantServer:
    """Shared state + behaviour of a Music Assistant 2.10.7 server (see the module docstring).

    State a test may read or set directly:
      ``players`` / ``queues`` / ``queue_items`` (server-side models, live), ``providers``,
      ``valid_tokens``, ``users`` (username -> password for login), ``guest_users``,
      ``reachable``, ``setup_required`` (fresh MA: websocket closes after "Setup required",
      login answers 403), ``hang_init`` (the last initial-state command is never answered, so
      ``init_ready`` stays unset while the caches are filled), ``command_yields`` (event loop
      yields before each command runs), ``player_updates_after_result``, ``schema_version``,
      ``min_schema_version``, ``server_version``.
    Test inputs:
      ``fail_next[command] = MediaNotFoundError("...")`` (one shot; also ``DROP``,
      ``DROP_HARD``, ``HANG``), ``unplayable_track_ids`` (resolve with ``available=False``, so
      play_media filters them out), ``stream_failure_track_ids`` (accepted, but the stream fails
      when MA starts them), ``library_track_ids`` (come back as ``library://track/N`` items with
      the Qobuz id only in ``provider_mappings``), ``tracks[track_id] = FakeTrack(...)``.
    Logs:
      ``command_log`` (``(command, args)`` for every command a fake client sent, except
      ``auth``; args are JSON values, and enums compare equal to their ``StrEnum`` members),
      ``events`` (every event emitted, as ``MassEvent`` with dict data).
    """

    DROP = DROP
    DROP_HARD = DROP_HARD
    HANG = HANG

    def __init__(
        self,
        url: str = DEFAULT_URL,
        *,
        token: str = DEFAULT_TOKEN,
        server_version: str = SERVER_VERSION,
        schema_version: int = SCHEMA_VERSION,
        min_schema_version: int = MIN_SCHEMA_VERSION,
    ) -> None:
        self.url = url
        self.aliases: set[str] = set()  # other URLs that reach this server
        self.server_version = server_version
        self.schema_version = schema_version
        self.min_schema_version = min_schema_version
        self.token = token
        self.valid_tokens: set[str] = {token} if token else set()
        self.users: dict[str, str] = {"admin": "admin-password"}
        self.guest_users: set[str] = set()
        self.reachable = True
        self.setup_required = False

        self.players: dict[str, Player] = {}
        self.queues: dict[str, PlayerQueue] = {}
        self.queue_items: dict[str, list[QueueItem]] = {}
        self.providers: list[ProviderInstance] = []
        self.unplayable_track_ids: set[str] = set()
        self.stream_failure_track_ids: set[str] = set()
        self.library_track_ids: set[str] = set()
        self.dead_player_ids: set[str] = set()  # speakers that never start playing

        self.command_log: list[tuple[str, dict[str, Any]]] = []
        self.events: list[MassEvent] = []
        self.fail_next: dict[str, Any] = {}
        self.created_clients: list[FakeMusicAssistantClient] = []

        self._sockets: list[_ServerSocket] = []
        self._tasks: set[asyncio.Task[None]] = set()
        self._gates: dict[str, asyncio.Event] = {}
        self._gate_waiters = 0
        self._item_seq = 0
        self._token_seq = 0
        self._radio_seq = 0
        self._library_ids: dict[str, str] = {}
        self._tokens: dict[str, _TokenInfo] = {}
        self._rng = random.Random(0)
        self._prev_state: dict[str, Optional[dict[str, Any]]] = {}
        self._refcount: dict[str, int] = {}
        self._transitioning: dict[str, bool] = {}
        self._device_item: dict[str, Optional[str]] = {}
        self._device_media: dict[str, Optional[PlayerMedia]] = {}
        self._session_seq: dict[str, int] = {}
        self._dirty_players: set[str] = set()
        self._player_fingerprints: dict[str, str] = {}
        self._op_depth = 0
        self._client_class: Optional[type[FakeMusicAssistantClient]] = None
        self._commands: dict[str, Callable[..., Any]] = self._build_command_table()
        self._signatures = {name: inspect.signature(fn) for name, fn in self._commands.items()}
        if token:
            self._register_token(token, "admin", "Qobuz Bridge", long_lived=True)

    @property
    def client_class(self) -> type[FakeMusicAssistantClient]:
        """``MusicAssistantClient`` replacement bound to this server (real signature)."""
        if self._client_class is None:
            self._client_class = type(
                "BoundFakeMusicAssistantClient",
                (FakeMusicAssistantClient,),
                {"_fake_server": self},
            )
        return self._client_class

    def client_factory(
        self,
        server_url: str,
        aiohttp_session: Optional[aiohttp.ClientSession],
        token: Optional[str] = None,
    ) -> FakeMusicAssistantClient:
        """Build a new client bound to this server: ``(url, session, token) -> client``."""
        return self.client_class(server_url, aiohttp_session, token)

    @property
    def clients(self) -> list[FakeMusicAssistantClient]:
        """Clients whose websocket is open on the server side (connected, not yet dropped)."""
        result: list[FakeMusicAssistantClient] = []
        for sock in self._sockets:
            client = sock.connection.client
            if not sock.closed and client is not None and client not in result:
                result.append(client)
        return result

    async def connect_client(
        self, token: Optional[str] = None, *, timeout: float = 2.0
    ) -> tuple[FakeMusicAssistantClient, asyncio.Task[None]]:
        """Build a client, run ``start_listening`` in a task and wait for its initial state."""
        client = self.client_factory(self.url, None, self.token if token is None else token)
        ready = asyncio.Event()
        task = asyncio.create_task(client.start_listening(ready))
        ready_wait = asyncio.create_task(ready.wait())
        done, _ = await asyncio.wait(
            {task, ready_wait}, timeout=timeout, return_when=asyncio.FIRST_COMPLETED
        )
        if ready_wait not in done:
            ready_wait.cancel()
            if task in done:
                task.result()  # re-raise the connect error
            raise asyncio.TimeoutError("fake client did not finish its initial state fetch")
        return client, task

    async def settle(self, max_rounds: int = 200, quiet_rounds: int = 5) -> None:
        """Yield until commands, websocket frames and the callbacks they schedule have run.

        Returns after ``quiet_rounds`` event-loop iterations without server work or unread
        frames (commands held by :meth:`gate` do not count). It never sleeps, so code that
        waits on real timers (debounces, ``call_later``) needs those timers patched.
        """
        idle_rounds = 0
        for _ in range(max_rounds):
            await asyncio.sleep(0)
            pending_tasks = sum(1 for task in self._tasks if not task.done())
            busy = pending_tasks > self._gate_waiters or any(
                sock.inbox and sock.connection._receiving for sock in self._all_live_sockets()
            )
            if busy:
                idle_rounds = 0
                continue
            idle_rounds += 1
            if idle_rounds >= quiet_rounds:
                return

    def gate(self, command: str) -> asyncio.Event:
        """Hold every ``command`` (after logging it) until the returned event is set."""
        return self._gates.setdefault(command, asyncio.Event())

    def release(self, command: str) -> None:
        """Release and remove the gate for ``command``."""
        gate = self._gates.pop(command, None)
        if gate is not None:
            gate.set()

    def add_player(
        self,
        player_id: str,
        name: str,
        type: PlayerType = PlayerType.PLAYER,  # noqa: A002 - mirrors Player.type
        provider: str = "universal_player",
        available: bool = True,
        volume: int = 30,
        group_members: Iterable[str] = (),
        supported_features: Optional[Iterable[PlayerFeature]] = None,
        *,
        enabled: bool = True,
        hide_in_ui: bool = False,
    ) -> Player:
        """Register a player (and its queue, ``queue_id == player_id``) like MA does."""
        player_type = PlayerType(type)
        if supported_features is None:
            features = set(DEFAULT_PLAYER_FEATURES)
            if player_type == PlayerType.GROUP:
                features.add(PlayerFeature.SET_MEMBERS)
        else:
            features = set(supported_features)
        members = list(group_members)
        group_volume = volume
        if members:
            levels = [self.players[m].volume_level or 0 for m in members if m in self.players]
            if levels:
                group_volume = round(sum(levels) / len(levels))
        player = Player(
            player_id=player_id,
            provider=provider,
            type=player_type,
            name=name,
            available=available,
            device_info=DeviceInfo(),
            supported_features=features,
            playback_state=PlaybackState.IDLE,
            elapsed_time=0.0,
            elapsed_time_last_updated=time.time(),
            powered=True,
            volume_level=group_volume if player_type == PlayerType.GROUP else volume,
            volume_muted=False,
            group_members=UniqueList(members),
            enabled=enabled,
            hide_in_ui=hide_in_ui,
            group_volume=group_volume,
        )
        self.players[player_id] = player
        self._device_item[player_id] = None
        self._device_media[player_id] = None
        queue: Optional[PlayerQueue] = None
        if player_type != PlayerType.PROTOCOL:
            queue = PlayerQueue(
                queue_id=player_id,
                active=True,
                display_name=name,
                available=available,
                items=0,
            )
            self.queues[player_id] = queue
            self.queue_items[player_id] = []
            self._prev_state[player_id] = None
            self._refcount[player_id] = 0
            self._transitioning[player_id] = False
            self._session_seq[player_id] = 0
        with self._operation(flush=False):
            self._refresh_player(player_id)
            self._player_fingerprints[player_id] = self._fingerprint(player_id)
            self._emit(EventType.PLAYER_ADDED, player_id, player)
            if queue is not None:
                self._emit(EventType.QUEUE_ADDED, player_id, queue)
        return player

    def update_player(
        self,
        player_id: str,
        *,
        name: Optional[str] = None,
        available: Optional[bool] = None,
        enabled: Optional[bool] = None,
        hide_in_ui: Optional[bool] = None,
        group_members: Optional[Iterable[str]] = None,
    ) -> None:
        """Change player attributes (as a provider would) and emit PLAYER_UPDATED."""
        with self._operation():
            player = self.players[player_id]
            if name is not None:
                player.name = name
            if available is not None:
                player.available = available
            if enabled is not None:
                player.enabled = enabled
            if hide_in_ui is not None:
                player.hide_in_ui = hide_in_ui
            if group_members is not None:
                player.group_members = UniqueList(group_members)
            self._touch(player_id)
            if player_id in self.queues:
                self._flush_players()
                queue = self.queues[player_id]
                if queue.display_name != player.name or queue.available != player.available:
                    queue.display_name = player.name
                    queue.available = player.available
                    self._signal(player_id)

    def remove_player(self, player_id: str) -> None:
        """Unregister a player (PLAYER_REMOVED); its queue goes with it."""
        with self._operation():
            self.players.pop(player_id, None)
            self.queues.pop(player_id, None)
            self.queue_items.pop(player_id, None)
            self._dirty_players.discard(player_id)
            self._player_fingerprints.pop(player_id, None)
            self._emit(EventType.PLAYER_REMOVED, player_id, None)

    def add_qobuz_provider(
        self, available: bool = True, instance_id: str = QOBUZ_INSTANCE_ID
    ) -> ProviderInstance:
        """Add a Qobuz music provider instance (emits PROVIDERS_UPDATED)."""
        provider = ProviderInstance(
            type=ProviderType.MUSIC,
            domain="qobuz",
            name="Qobuz",
            instance_id=instance_id,
            supported_features=set(),
            available=available,
            is_streaming_provider=True,
            lookup_key=instance_id,
        )
        self.providers = [p for p in self.providers if p.instance_id != instance_id] + [provider]
        self._emit(EventType.PROVIDERS_UPDATED, None, list(self.providers))
        return provider

    def queue(self, queue_id: str) -> PlayerQueue:
        """Server-side queue (live object)."""
        return self.queues[queue_id]

    def item_ids(self, queue_id: str) -> list[str]:
        """``queue_item_id`` of every item, in queue order."""
        return [item.queue_item_id for item in self.queue_items[queue_id]]

    def track_ids(self, queue_id: str) -> list[Optional[str]]:
        """Provider track id of every item (``None`` for items without one)."""
        return [_item_track_id(item) for item in self.queue_items[queue_id]]

    def calls(self, command: str) -> list[dict[str, Any]]:
        """Args of every logged ``command`` sent by a fake client, oldest first."""
        return [args for name, args in self.command_log if name == command]

    @property
    def writes(self) -> list[tuple[str, dict[str, Any]]]:
        """``command_log`` without read-only commands: what a client asked MA to *do*."""
        return [(name, args) for name, args in self.command_log if name not in READ_COMMANDS]

    def clear_logs(self) -> None:
        """Forget ``command_log`` and ``events``."""
        self.command_log.clear()
        self.events.clear()

    def start_playing(
        self,
        queue_id: str,
        track_ids: Union[str, Iterable[str]],
        option: QueueOption = QueueOption.REPLACE,
        *,
        scheme: str = "qobuz",
    ) -> list[QueueItem]:
        """Another client runs ``play_media(queue_id, ["<scheme>://track/<id>", ...], option)``.

        Returns copies of the queue items it created.
        """
        ids = [track_ids] if isinstance(track_ids, str) else list(track_ids)
        with self._operation():
            before = set(self.item_ids(queue_id))
            self._handle_play_media(queue_id, [f"{scheme}://track/{t}" for t in ids], option)
            return [
                copy.deepcopy(item)
                for item in self.queue_items[queue_id]
                if item.queue_item_id not in before
            ]

    def buffer_next(self, queue_id: str) -> Optional[QueueItem]:
        """The speaker started fetching the next item (``index_in_buffer = current + 1``).

        Afterwards ``delete_item`` of that item is silently ignored and NEXT / REPLACE_NEXT
        insert after it. Returns a copy of the buffered item, or ``None`` if there is none.
        """
        with self._operation():
            queue = self.queues[queue_id]
            if queue.current_index is None:
                return None
            nxt = self._get_next_item(queue_id, queue.current_index)
            if nxt is None:
                return None
            index = self._index_by_id(queue_id, nxt.queue_item_id)
            assert index is not None
            self._track_loaded_in_buffer(queue_id, index)
            return copy.deepcopy(nxt)

    def advance_time(self, queue_id: str, seconds: float) -> None:
        """Let ``seconds`` of playback pass on a playing queue (no event; MA sends no ticks).

        The server's elapsed time moves forward, and every connected client's cached anchors
        (and any frames still queued for it) are shifted back, so
        ``corrected_elapsed_time`` advances exactly as if the wall clock had moved.
        Paused or idle queues do not move.
        """
        queue = self.queues[queue_id]
        player = self.players.get(queue_id)
        if player is not None and player.playback_state == PlaybackState.PLAYING:
            player.elapsed_time = (player.elapsed_time or 0.0) + seconds
        if queue.state == PlaybackState.PLAYING:
            queue.elapsed_time += seconds
        for sock in self._all_live_sockets():
            client = sock.connection.client
            if client is not None:
                cached_queue = client.player_queues.get(queue_id)
                if cached_queue is not None and cached_queue.state == PlaybackState.PLAYING:
                    cached_queue.elapsed_time_last_updated -= seconds
                cached_player = client.players.get(queue_id)
                if (
                    cached_player is not None
                    and cached_player.playback_state == PlaybackState.PLAYING
                ):
                    if cached_player.elapsed_time_last_updated is not None:
                        cached_player.elapsed_time_last_updated -= seconds
                    media = cached_player.current_media
                    if media is not None and media.elapsed_time_last_updated is not None:
                        media.elapsed_time_last_updated -= seconds

    def finish_current_track(self, queue_id: str) -> None:
        """The current track ends naturally on the speaker.

        If a next item exists, the speaker moves on to it (the buffered item if
        :meth:`buffer_next` ran, otherwise it buffers the next one first): QUEUE_UPDATED with
        the new current item, state PLAYING, elapsed 0. Otherwise the speaker goes idle
        (QUEUE_UPDATED state IDLE, ``ended`` still False). Call :meth:`mark_ended` for MA's
        end-of-queue settle that follows about 5 s later.
        """
        with self._operation():
            queue = self.queues[queue_id]
            player = self.players[queue_id]
            if not (
                queue.active
                and player.playback_state == PlaybackState.PLAYING
                and queue.current_index is not None
            ):
                raise RuntimeError("finish_current_track() needs a queue that is playing an item")
            current = queue.current_index
            buffered = queue.index_in_buffer
            next_index: Optional[int]
            if (
                buffered is not None
                and buffered > current
                and self._get_item(queue_id, buffered) is not None
            ):
                next_index = buffered
            else:
                nxt = self._get_next_item(queue_id, current)
                next_index = self._index_by_id(queue_id, nxt.queue_item_id) if nxt else None
                if next_index is not None and next_index != buffered:
                    self._track_loaded_in_buffer(queue_id, next_index)
            if next_index is None:
                duration = queue.current_item.duration if queue.current_item else None
                player.elapsed_time = float(duration or player.corrected_elapsed_time or 0.0)
                player.elapsed_time_last_updated = time.time()
                player.playback_state = PlaybackState.IDLE
                self._touch(queue_id)
                self._on_player_update(queue_id)
                return
            item = self.queue_items[queue_id][next_index]
            self._device_play(queue_id, item, 0.0)
            self._on_player_update(queue_id)

    def mark_ended(self, queue_id: str) -> bool:
        """MA's end-of-queue settle (runs ~5 s after the last track went idle).

        Like ``playback_tracker._settle_or_resume_delayed``: does nothing if the queue is no
        longer idle, starts the next item if one was added meanwhile ("Items added to queue
        while idle, resuming playback"), and otherwise marks the queue ended: ``ended=True``,
        current item parked on the last item, elapsed 0, ``index_in_buffer`` None. Returns
        True when the queue was marked ended.
        """
        with self._operation():
            queue = self.queues[queue_id]
            if queue.state != PlaybackState.IDLE:
                return False
            if queue.current_index is not None:
                nxt = self._get_next_item(queue_id, queue.current_index)
                if nxt is not None:
                    with self._play_action(queue_id):
                        self._play_index(queue_id, nxt.queue_item_id)
                    return False
            items = self.queue_items[queue_id]
            if not items:
                self._clear(queue_id)
                return True
            queue.ended = True
            queue.current_index = len(items) - 1
            queue.current_item = items[-1]
            queue.next_item = None
            queue.elapsed_time = 0.0
            queue.elapsed_time_last_updated = time.time()
            queue.index_in_buffer = None
            queue.resume_pos = 0
            self._signal(queue_id)
            return True

    def ui_pause(self, queue_id: str) -> None:
        """Someone presses pause in MA's UI (``player_queues/pause``)."""
        with self._operation():
            self._pause(queue_id)

    def ui_resume(self, queue_id: str) -> None:
        """Someone presses play in MA's UI (``player_queues/play``: unpause or resume)."""
        with self._operation():
            self._handle_play(queue_id)

    def ui_stop(self, queue_id: str) -> None:
        """Someone presses stop in MA's UI (``player_queues/stop``)."""
        with self._operation():
            self._handle_stop(queue_id)

    def ui_clear(self, queue_id: str) -> None:
        """Someone clears the queue in MA's UI (``player_queues/clear``)."""
        with self._operation():
            self._clear(queue_id)

    def ui_next(self, queue_id: str) -> None:
        """Someone presses next in MA's UI (no-op at the end of the queue)."""
        with self._operation():
            self._next(queue_id)

    def ui_previous(self, queue_id: str) -> None:
        """Someone presses previous in MA's UI (restart if elapsed >= 5 s, else index - 1)."""
        with self._operation():
            self._previous(queue_id)

    def ui_seek(self, queue_id: str, seconds: int) -> None:
        """Someone seeks in MA's UI (stream restarts at the position; QUEUE_TIME_UPDATED)."""
        with self._operation():
            self._seek(queue_id, seconds)

    def auto_stop_after_pause(self, queue_id: str) -> bool:
        """MA's watchdog stops a queue paused for 30 s: state IDLE, same item, resume_pos kept."""
        with self._operation():
            queue = self.queues[queue_id]
            if queue.state != PlaybackState.PAUSED:
                return False
            self._handle_stop(queue_id)
            return True

    def user_plays_something_else(
        self,
        queue_id: str,
        name: str = "Radio",
        *,
        track_ids: Optional[Iterable[str]] = None,
    ) -> list[QueueItem]:
        """Another client replaces the queue with something else and it starts playing.

        Default: one radio station called ``name`` (not Qobuz). With ``track_ids`` the
        replacement is Qobuz tracks, i.e. someone played Qobuz from MA's own UI. Either way
        the items get new ids. Returns copies of the new items.
        """
        media: list[Any]
        if track_ids is not None:
            media = [f"qobuz://track/{t}" for t in track_ids]
        else:
            self._radio_seq += 1
            radio_id = f"radio{self._radio_seq}"
            media = [
                Radio(
                    item_id=radio_id,
                    provider="tunein",
                    name=name,
                    provider_mappings={
                        ProviderMapping(
                            item_id=radio_id, provider_domain="tunein", provider_instance="tunein"
                        )
                    },
                )
            ]
        with self._operation():
            self._handle_play_media(queue_id, media, QueueOption.REPLACE)
            return [copy.deepcopy(item) for item in self.queue_items[queue_id]]

    def switch_source(self, player_id: str, source: Optional[str] = "spotify") -> None:
        """The speaker switches to another source (PLAYER_UPDATED, then QUEUE_UPDATED).

        For an external source, the player plays it, ``player.active_source = source`` and the
        queue goes ``active=False`` / IDLE. Like MA, the QUEUE_UPDATED only comes when that
        changes the queue's state, item or position (a playing/paused queue). An idle queue
        goes inactive silently: only the PLAYER_UPDATED tells. ``source`` equal to the player id
        (or None) switches back to MA, with the speaker idle.
        """
        with self._operation():
            player = self.players[player_id]
            player.active_source = source
            if source in (player_id, None):
                self._device_media[player_id] = None
                if player.playback_state == PlaybackState.PLAYING:
                    self._device_set_state(player_id, PlaybackState.IDLE)
            else:
                self._device_media[player_id] = PlayerMedia(
                    uri=f"{source}://current",
                    media_type=MediaType.UNKNOWN,
                    title=f"{source} stream",
                    source_id=source,
                )
                self._device_item[player_id] = None
                player.playback_state = PlaybackState.PLAYING
                player.elapsed_time = 0.0
                player.elapsed_time_last_updated = time.time()
            self._touch(player_id)
            self._flush_players()
            if player_id in self.queues:
                self._on_player_update(player_id)

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

    def set_player_volume(self, player_id: str, level: int) -> None:
        """Volume changed in MA's UI / on the device (PLAYER_UPDATED; groups: group_volume)."""
        with self._operation():
            self._set_volume(player_id, level)

    def drop_connections(self, graceful: bool = True) -> None:
        """Close every websocket: ``start_listening`` returns None, or raises ConnectionFailed."""
        for sock in list(self._sockets):
            sock.close(graceful)

    def restart(self) -> None:
        """MA restarts (add-on update, crash): the music stops, items and ids survive.

        Like MA 2.10.7 ``mass.stop()``: player providers unload first (connected clients get
        PLAYER_UPDATED ``available=False`` for every player), then the websockets close
        gracefully. Every queue comes back from MA's cache stopped: state IDLE, same items,
        ids and current item, ``index_in_buffer`` None, ``resume_pos`` at the position it
        stopped at. The server is unreachable until :meth:`come_back`.
        """
        with self._operation():
            for player_id, player in self.players.items():
                player.available = False
                self._touch(player_id)
        self.drop_connections(graceful=True)
        for queue_id, queue in self.queues.items():
            player = self.players.get(queue_id)
            if queue.state in (PlaybackState.PLAYING, PlaybackState.PAUSED):
                queue.elapsed_time = float(queue.corrected_elapsed_time)
                queue.resume_pos = int(queue.elapsed_time)
            queue.state = PlaybackState.IDLE
            queue.index_in_buffer = None
            queue.extra_attributes[ATTR_PLAY_ACTION_IN_PROGRESS] = False
            self._refcount[queue_id] = 0
            self._transitioning[queue_id] = False
            self._prev_state[queue_id] = None
            if player is not None and player.playback_state != PlaybackState.IDLE:
                if player.active_source in (queue_id, None):
                    self._device_set_state(queue_id, PlaybackState.IDLE)
        self._dirty_players.clear()
        self.reachable = False

    def come_back(self, *, players_available: bool = True) -> None:
        """The restarted server accepts connections again.

        By default every player has re-registered (available again). With
        ``players_available=False`` they are still unavailable (MA re-registers players slowly);
        flip them back later with ``update_player(player_id, available=True)``.
        """
        if players_available:
            for player_id, player in self.players.items():
                player.available = True
                if player_id in self.queues:
                    self.queues[player_id].available = True
                self._player_fingerprints[player_id] = self._fingerprint(player_id)
        self.reachable = True

    def set_reachable(self, reachable: bool, *, drop: bool = False) -> None:
        """Make new connections fail (or work); ``drop=True`` also kills open ones (error)."""
        self.reachable = reachable
        if drop:
            self.drop_connections(graceful=False)

    def revoke_token(self, token: str) -> None:
        """Revoke a token: its sockets are closed at once, and reconnects fail authentication."""
        self.valid_tokens.discard(token)
        self._tokens.pop(token, None)
        for sock in list(self._sockets):
            if sock.token == token:
                sock.close(graceful=True)

    async def login(
        self,
        server_url: str,
        username: str,
        password: str,
        aiohttp_session: Optional[aiohttp.ClientSession] = None,
        ssl_context: Any = None,
    ) -> tuple[User, str]:
        """Fake ``music_assistant_client.login`` (POST /auth/login)."""
        await asyncio.sleep(0)
        self._check_http(server_url)
        if self.setup_required:
            raise CannotConnect(Exception("Login failed with status 403"))
        if username not in self.users or self.users[username] != password:
            raise LoginFailed("Invalid username or password")
        self._token_seq += 1
        session_token = f"fake-session-token-{self._token_seq}"
        self._register_token(session_token, username, "session", long_lived=False)
        return self._user(username), session_token

    def _check_http(self, server_url: str) -> None:
        if not self.reachable or not self._url_matches(server_url):
            parsed = urlparse(server_url)
            err = aiohttp.ClientConnectionError(
                f"Cannot connect to host {parsed.hostname}:{parsed.port} ssl:default "
                "[Connect call failed]"
            )
            raise CannotConnect(err) from err

    def _all_live_sockets(self) -> list[_ServerSocket]:
        """Sockets whose client may still read frames (open, or closed but not yet read)."""
        result = list(self._sockets)
        for client in self.created_clients:
            connection = client.connection
            sock = getattr(connection, "_socket", None)
            if isinstance(sock, _ServerSocket) and sock not in result:
                result.append(sock)
        return result

    def _url_matches(self, url: str) -> bool:
        try:
            wanted = urlparse(get_websocket_url(url)).netloc
        except RuntimeError:
            return False
        known = {urlparse(get_websocket_url(u)).netloc for u in {self.url, *self.aliases}}
        return wanted in known

    def _accept(self, connection: FakeWebsocketsConnection) -> _ServerSocket:
        if not self.reachable or not self._url_matches(connection.ws_server_url):
            parsed = urlparse(connection.ws_server_url)
            err = aiohttp.ClientConnectionError(
                f"Cannot connect to host {parsed.hostname}:{parsed.port} ssl:default "
                "[Connect call failed]"
            )
            raise CannotConnect(err) from err
        sock = _ServerSocket(self, connection)
        self._sockets.append(sock)
        sock.push(self._server_info_dict())
        if self.setup_required:
            sock.push({"message_id": "connection", "error_code": 503, "details": "Setup required"})
            sock.close(graceful=True)
        return sock

    def _forget_socket(self, sock: _ServerSocket) -> None:
        if sock in self._sockets:
            self._sockets.remove(sock)

    def _server_info_dict(self) -> dict[str, Any]:
        info = ServerInfoMessage(
            server_id="fakemaserver",
            server_version=self.server_version,
            schema_version=self.schema_version,
            min_supported_schema_version=self.min_schema_version,
            base_url=self.url,
            homeassistant_addon=True,
            onboard_done=not self.setup_required,
            name="Music Assistant",
            status=CoreState.RUNNING,
        )
        result: dict[str, Any] = _wire(info.to_dict())
        return result

    def _schedule_command(self, sock: _ServerSocket, raw: dict[str, Any]) -> None:
        task = asyncio.get_running_loop().create_task(self._run_command(sock, raw))
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)

    async def _run_command(self, sock: _ServerSocket, raw: dict[str, Any]) -> None:
        message_id = raw.get("message_id")
        command = str(raw.get("command", ""))
        args: dict[str, Any] = dict(raw.get("args") or {})
        await asyncio.sleep(0)
        if command == "auth":
            sock.push(self._handle_auth(sock, message_id, args))
            return
        self.command_log.append((command, copy.deepcopy(args)))
        gate = self._gates.get(command)
        if gate is not None and not gate.is_set():
            self._gate_waiters += 1
            try:
                await gate.wait()
            finally:
                self._gate_waiters -= 1
        if command in self.fail_next:
            failure = self.fail_next.pop(command)
            if failure == DROP:
                sock.close(graceful=True)
                return
            if failure == DROP_HARD:
                sock.close(graceful=False)
                return
            if failure == HANG:
                return
            if not isinstance(failure, BaseException):
                raise TypeError(f"fail_next[{command!r}] must be an exception or DROP/HANG")
            sock.push(_exception_frame(message_id, failure))
            return
        handler = self._commands.get(command)
        if handler is None:
            sock.push(
                _error_frame(message_id, InvalidCommand.error_code, f"Invalid command: {command}")
            )
            return
        frame: dict[str, Any]
        try:
            with self._operation(flush=False):
                kwargs = self._bind_args(command, args)
                result = handler(sock, **kwargs)
        except Exception as err:  # noqa: BLE001 - the real server answers every error
            frame = _exception_frame(message_id, err)
        else:
            frame = _wire(SuccessResultMessage(message_id, result).to_dict())
        sock.push(frame)
        self._flush_players()  # after the result, like MA's 0.25 s debounce

    def _bind_args(self, command: str, args: dict[str, Any]) -> dict[str, Any]:
        """Like MA's ``parse_arguments(strict=False)``: extra args are ignored."""
        params = list(self._signatures[command].parameters.values())[1:]  # skip sock
        kwargs: dict[str, Any] = {}
        for param in params:
            value = args.get(param.name)
            if value is not None:
                kwargs[param.name] = value
            elif param.default is not inspect.Parameter.empty:
                kwargs[param.name] = param.default
            else:
                raise KeyError(f"`{param.name}` is required.")
        return kwargs

    def _handle_auth(
        self, sock: _ServerSocket, message_id: Any, args: dict[str, Any]
    ) -> dict[str, Any]:
        token = args.get("token") or args.get("access_token")
        if not token:
            return _error_frame(
                message_id, AuthenticationRequired.error_code, "Authentication token required"
            )
        if token not in self.valid_tokens:
            return _error_frame(message_id, InvalidToken.error_code, "Invalid or expired token")
        info = self._register_token(token, "admin", "token", long_lived=True)
        sock.authenticated = True
        sock.token = token
        sock.username = info.username
        result = {"authenticated": True, "user": self._user(info.username).to_dict()}
        return _wire(SuccessResultMessage(message_id, result).to_dict())

    def _register_token(self, token: str, username: str, name: str, long_lived: bool) -> _TokenInfo:
        """Return the token's metadata (tokens added straight to ``valid_tokens`` get some)."""
        self.valid_tokens.add(token)
        info = self._tokens.get(token)
        if info is None:
            info = _TokenInfo(f"tokenid{len(self._tokens) + 1}", username, name, long_lived)
            self._tokens[token] = info
        return info

    def _user(self, username: str) -> User:
        role = "guest" if username in self.guest_users else "admin"
        return User(user_id=f"user-{username}", username=username, role=role)

    @contextmanager
    def _operation(self, flush: bool = True) -> Iterator[None]:
        """Group state changes; PLAYER_UPDATED is coalesced and emitted when the group ends."""
        self._op_depth += 1
        try:
            yield
        finally:
            self._op_depth -= 1
            if self._op_depth == 0 and flush:
                self._flush_players()

    def _emit(self, event: EventType, object_id: Optional[str], data: Any) -> None:
        wire = json_dumps(MassEvent(event=event, object_id=object_id, data=data).to_dict())
        self.events.append(
            MassEvent(event=event, object_id=object_id, data=json_loads(wire)["data"])
        )
        for sock in list(self._sockets):
            if sock.authenticated and not sock.closed:
                sock.push(json_loads(wire))

    def _touch(self, player_id: str) -> None:
        self._dirty_players.add(player_id)

    def _flush_players(self) -> None:
        dirty = sorted(self._dirty_players)
        self._dirty_players.clear()
        for player_id in dirty:
            if player_id not in self.players:
                continue
            self._refresh_player(player_id)
            fingerprint = self._fingerprint(player_id)
            if fingerprint == self._player_fingerprints.get(player_id):
                continue
            self._player_fingerprints[player_id] = fingerprint
            self._emit(EventType.PLAYER_UPDATED, player_id, self.players[player_id])

    def _fingerprint(self, player_id: str) -> str:
        data = self.players[player_id].to_dict()
        # like MA's player fingerprint: position-only changes are not an update
        data.pop("elapsed_time", None)
        data.pop("elapsed_time_last_updated", None)
        if isinstance(data.get("current_media"), dict):
            data["current_media"].pop("elapsed_time", None)
            data["current_media"].pop("elapsed_time_last_updated", None)
        return json_dumps(data)

    def _refresh_player(self, player_id: str) -> None:
        """Derive ``current_media`` like MA: the queue's current item while the queue is active."""
        player = self.players[player_id]
        queue = self.queues.get(player_id)
        if (
            queue is not None
            and player.active_source in (player_id, None)
            and queue.current_item is not None
        ):
            item = queue.current_item
            media_item = item.media_item
            album = getattr(media_item, "album", None)
            player.current_media = PlayerMedia(
                uri=str(media_item.uri) if media_item is not None else item.uri,
                media_type=item.media_type,
                title=media_item.name if media_item is not None else item.name,
                artist=getattr(media_item, "artist_str", None),
                album=album.name if album is not None else None,
                duration=item.duration,
                source_id=player_id,
                queue_item_id=item.queue_item_id,
                elapsed_time=int(queue.elapsed_time),
                elapsed_time_last_updated=queue.elapsed_time_last_updated,
            )
        else:
            media = self._device_media.get(player_id)
            player.current_media = copy.deepcopy(media) if media is not None else None

    def _signal(self, queue_id: str, items_changed: bool = False) -> None:
        """``controller.signal_update``: QUEUE_ITEMS_UPDATED (if items changed), QUEUE_UPDATED."""
        queue = self.queues[queue_id]
        if items_changed:
            self._emit(EventType.QUEUE_ITEMS_UPDATED, queue_id, queue)
        self._emit(EventType.QUEUE_UPDATED, queue_id, queue)
        self._touch(queue_id)

    @contextmanager
    def _play_action(self, queue_id: str) -> Iterator[None]:
        """``helpers.handle_play_action``: flags the queue busy while a play action runs."""
        queue = self.queues.get(queue_id)
        if queue is None:
            yield
            return
        prev_in_progress = bool(queue.extra_attributes.get(ATTR_PLAY_ACTION_IN_PROGRESS, False))
        self._refcount[queue_id] += 1
        queue.extra_attributes[ATTR_PLAY_ACTION_IN_PROGRESS] = True
        if not prev_in_progress:
            self._signal(queue_id)
        try:
            yield
        finally:
            self._refcount[queue_id] -= 1
            if self._refcount[queue_id] <= 0:
                self._refcount[queue_id] = 0
                queue.extra_attributes[ATTR_PLAY_ACTION_IN_PROGRESS] = False
                if queue_id in self.players:
                    self._on_player_update(queue_id)
                self._signal(queue_id)

    def _on_player_update(self, queue_id: str) -> None:
        """``controller.on_player_update`` for the queue of ``queue_id``."""
        player = self.players.get(queue_id)
        queue = self.queues.get(queue_id)
        if player is None or queue is None or player.type == PlayerType.PROTOCOL:
            return
        queue.active = player.active_source in (queue_id, None)
        if not queue.active and self._prev_state.get(queue_id) is None:
            queue.state = PlaybackState.IDLE
            return
        if self._transitioning[queue_id]:
            return
        self._update_queue_from_player(queue_id)

    def _update_queue_from_player(self, queue_id: str) -> None:
        """``playback_tracker._update_queue_from_player`` (non-flow mode)."""
        player = self.players[queue_id]
        queue = self.queues[queue_id]
        queue.display_name = player.name
        queue.available = player.available
        queue.items = len(self.queue_items[queue_id])
        queue.state = (
            (player.playback_state or PlaybackState.IDLE) if queue.active else PlaybackState.IDLE
        )
        if queue.active and queue.state in (PlaybackState.PLAYING, PlaybackState.PAUSED):
            item_id = self._device_item.get(queue_id)
            if item_id is None or self._get_item(queue_id, item_id) is None:
                return  # player still transitioning (or reports an item we no longer hold)
            index = self._index_by_id(queue_id, item_id)
            queue.current_index = index
            queue.current_item = self._get_item(queue_id, index)
            queue.next_item = self._get_next_item(queue_id, index) if index is not None else None
            queue.elapsed_time = float(player.corrected_elapsed_time or 0.0)
            queue.elapsed_time_last_updated = time.time()
        elif queue.current_item is None and queue.current_index is not None:
            queue.current_item = self._get_item(queue_id, queue.current_index)
            queue.next_item = (
                self._get_next_item(queue_id, queue.current_index) if queue.current_item else None
            )
        prev = self._prev_state.get(queue_id) or {
            "state": PlaybackState.IDLE,
            "item": None,
            "elapsed": 0,
            "last_playing": 0,
        }
        current_item_id = queue.current_item.queue_item_id if queue.current_item else None
        if queue.state == PlaybackState.PLAYING:
            current_elapsed = int(queue.corrected_elapsed_time)
            if current_item_id != prev["item"]:
                last_playing = current_elapsed
            else:
                last_playing = max(current_elapsed, prev["last_playing"])
        else:
            last_playing = prev["last_playing"]
        new = {
            "state": queue.state,
            "item": current_item_id,
            "elapsed": int(queue.elapsed_time),
            "last_playing": last_playing,
        }
        changed = {key for key in ("state", "item", "elapsed") if prev[key] != new[key]}
        self._prev_state[queue_id] = new if queue.active else None
        if not changed:
            return
        if changed == {"elapsed"}:
            if abs(new["elapsed"] - prev["elapsed"]) > 2:
                self._emit(EventType.QUEUE_TIME_UPDATED, queue_id, queue.elapsed_time)
                self._touch(queue_id)
            return
        self._signal(queue_id)

    def _index_by_id(self, queue_id: str, queue_item_id: str) -> Optional[int]:
        for index, item in enumerate(self.queue_items.get(queue_id, [])):
            if item.queue_item_id == queue_item_id:
                return index
        return None

    def _get_item(
        self, queue_id: str, item_id_or_index: Union[int, str, None]
    ) -> Optional[QueueItem]:
        if item_id_or_index is None:
            return None
        items = self.queue_items.get(queue_id, [])
        if isinstance(item_id_or_index, int):
            return items[item_id_or_index] if 0 <= item_id_or_index < len(items) else None
        return next((x for x in items if x.queue_item_id == item_id_or_index), None)

    def _get_next_index(
        self,
        queue_id: str,
        cur_index: Optional[int],
        is_skip: bool = False,
        allow_repeat: bool = True,
    ) -> Optional[int]:
        queue = self.queues[queue_id]
        items = self.queue_items[queue_id]
        if not items or cur_index is None:
            return None
        if queue.repeat_mode == RepeatMode.ONE and not is_skip:
            return cur_index if allow_repeat else None
        if cur_index >= len(items) - 1:
            if allow_repeat and queue.repeat_mode == RepeatMode.ALL:
                return 0
            return None
        return cur_index + 1

    def _get_next_item(self, queue_id: str, cur_index: int) -> Optional[QueueItem]:
        for skip in range(5):
            next_index = self._get_next_index(queue_id, cur_index + skip)
            if next_index is None:
                break
            next_item = self._get_item(queue_id, next_index)
            if next_item is None or not next_item.available:
                continue
            return next_item
        return None

    def _insert_index(self, queue: PlayerQueue) -> int:
        """``committed_index + 1`` while playing/paused, else ``(current_index or 0) + 1``."""
        if queue.state in (PlaybackState.PLAYING, PlaybackState.PAUSED):
            boundary = committed_index(queue)
            cur_index = boundary if boundary is not None else 0
        else:
            cur_index = queue.current_index or 0
        return cur_index + 1

    def _track_loaded_in_buffer(self, queue_id: str, index: int) -> None:
        self.queues[queue_id].index_in_buffer = index
        self._signal(queue_id)

    def _update_items(self, queue_id: str, items: list[QueueItem]) -> None:
        self.queue_items[queue_id] = items
        queue = self.queues[queue_id]
        queue.items = len(items)
        current = queue.current_item
        if (
            current is not None
            and queue.index_in_buffer is not None
            and self._index_by_id(queue_id, current.queue_item_id) is None
        ):
            pass
        self._signal(queue_id, items_changed=True)

    def _load(
        self,
        queue_id: str,
        new_items: list[QueueItem],
        insert_at_index: int = 0,
        keep_remaining: bool = True,
        keep_played: bool = True,
    ) -> None:
        current = self.queue_items[queue_id]
        prev_items = current[:insert_at_index] if keep_played else []
        next_items = list(new_items)
        if keep_remaining:
            next_items += current[insert_at_index:]
        for index, item in enumerate(next_items):
            item.sort_index += insert_at_index + index
        self._update_items(queue_id, prev_items + next_items)

    def _ensure_current_index(self, queue_id: str) -> None:
        queue = self.queues[queue_id]
        if queue.current_index is not None:
            return
        queue.current_index = 0
        queue.current_item = self._get_item(queue_id, 0)
        self._signal(queue_id)

    def _clear(self, queue_id: str, skip_stop: bool = False) -> None:
        queue = self.queues[queue_id]
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
            # MA schedules this stop as a task: it lands after the clear's own events
            self._handle_stop(queue_id)

    def _handle_play_media(
        self,
        queue_id: str,
        media: Any,
        option: Optional[Union[QueueOption, str]] = None,
    ) -> None:
        """``controller.play_media`` + ``queue_loader._handle_play_media`` (linear queues)."""
        if queue_id not in self.queues:
            raise PlayerUnavailableError(f"Queue {queue_id} is not available")
        queue = self.queues[queue_id]
        with self._play_action(queue_id):
            self._transitioning[queue_id] = False
            self._get_player(queue_id, raise_unavailable=True)
            media_list = media if isinstance(media, list) else [media]
            resolved_option = QueueOption(option) if option is not None else None
            resolved: list[MediaItem] = []
            for entry in media_list:
                try:
                    media_item = self._resolve_media(entry)
                except MusicAssistantError as err:
                    logger.debug("Skipping %s: %s", entry, err)
                    continue
                if resolved_option is None:
                    resolved_option = (
                        QueueOption.REPLACE
                        if media_item.media_type == MediaType.RADIO
                        else QueueOption.PLAY
                    )
                resolved.append(media_item)
            queue_items = [
                self._build_queue_item(queue_id, item) for item in resolved if item.available
            ]
            if not queue_items:
                raise MediaNotFoundError("No playable items found")
            assert resolved_option is not None
            self._enqueue_with_option(queue_id, queue, queue_items, resolved_option)

    def _enqueue_with_option(
        self,
        queue_id: str,
        queue: PlayerQueue,
        queue_items: list[QueueItem],
        option: QueueOption,
    ) -> None:
        """``queue_loader._enqueue_with_option``."""
        continues_ended_queue = queue.ended and option == QueueOption.ADD
        if queue.ended and not continues_ended_queue and option != QueueOption.REPLACE:
            self._clear(queue_id, skip_stop=True)
        insert_at_index = self._insert_index(queue)

        if option == QueueOption.REPLACE:
            queue.index_in_buffer = None
            queue.ended = False
            queue.is_dynamic = False
            self._load(queue_id, queue_items, keep_remaining=False, keep_played=False)
            self._play_index(queue_id, 0)
            return
        if option == QueueOption.NEXT:
            self._load(queue_id, queue_items, insert_at_index=insert_at_index)
            self._ensure_current_index(queue_id)
            return
        if option == QueueOption.REPLACE_NEXT:
            self._load(
                queue_id,
                queue_items,
                insert_at_index=insert_at_index,
                keep_remaining=False,
            )
            self._ensure_current_index(queue_id)
            return
        if option == QueueOption.PLAY:
            play_at_index = 0 if queue.current_index is None else insert_at_index
            self._load(queue_id, queue_items, insert_at_index=play_at_index)
            next_index = min(play_at_index, len(self.queue_items[queue_id]) - 1)
            self._play_index(queue_id, next_index)
            return
        if option == QueueOption.ADD:
            self._load(queue_id, queue_items, insert_at_index=len(self.queue_items[queue_id]) + 1)
            self._ensure_current_index(queue_id)
            return
        raise InvalidDataError(f"Unsupported enqueue option: {option}")

    def _play_index(self, queue_id: str, index: Union[int, str], seek_position: float = 0) -> None:
        """Body of ``controller.play_index`` (call it inside :meth:`_play_action`)."""
        queue = self.queues[queue_id]
        queue.resume_pos = 0
        if isinstance(index, str):
            resolved = self._index_by_id(queue_id, index)
            if resolved is None:
                raise InvalidDataError(f"Item {index} not found in queue")
            index = resolved
        index = int(index)
        queue.index_in_buffer = index
        self._get_player(queue_id, raise_unavailable=True)
        self._session_seq[queue_id] += 1
        self._transitioning[queue_id] = True
        try:
            item: Optional[QueueItem] = None
            for attempt in range(5):
                item = self._get_item(queue_id, index)
                if item is None:
                    continue
                if _item_track_id(item) in self.stream_failure_track_ids:
                    item.available = False
                    next_index = self._get_next_index(queue_id, index, allow_repeat=False)
                    if next_index is None:
                        self._handle_stop(queue_id)
                        raise MediaNotFoundError(
                            f"Playback failed for {item.name} - no more tracks available"
                        )
                    index = next_index
                    continue
                queue.current_index = index
                queue.current_item = item
                queue.ended = False
                queue.elapsed_time = float(seek_position if attempt == 0 else 0)
                queue.elapsed_time_last_updated = time.time()
                prev = self._prev_state.get(queue_id)
                if prev is not None and prev["item"] == item.queue_item_id:
                    prev["last_playing"] = int(queue.elapsed_time)
                break
            else:
                self._handle_stop(queue_id)
                raise MediaNotFoundError("No playable item found to start playback")
            assert item is not None
            queue.flow_mode = False
            self._device_play(queue_id, item, queue.elapsed_time)
            queue.current_index = index
            queue.current_item = item
            self._signal(queue_id)
        finally:
            self._transitioning[queue_id] = False

    def _handle_stop(self, queue_id: str) -> None:
        if queue_id not in self.queues:
            raise PlayerUnavailableError(f"Queue {queue_id} is not available")
        queue = self.queues[queue_id]
        with self._play_action(queue_id):
            self._transitioning[queue_id] = False
            if queue.active and queue.state == PlaybackState.PLAYING:
                queue.resume_pos = int(queue.corrected_elapsed_time)
            player = self.players.get(queue_id)
            if player is not None and player.playback_state != PlaybackState.IDLE:
                self._device_set_state(queue_id, PlaybackState.IDLE)

    def _pause(self, queue_id: str) -> None:
        if queue_id not in self.queues:
            return
        self._transitioning[queue_id] = False
        queue = self.queues[queue_id]
        if queue.active and queue.state == PlaybackState.PLAYING:
            queue.resume_pos = int(queue.corrected_elapsed_time)
        player = self.players[queue_id]
        if player.playback_state == PlaybackState.PLAYING:
            if PlayerFeature.PAUSE not in player.supported_features:
                # MA falls back to stop for players that cannot pause
                self._handle_stop(queue_id)
                return
            self._device_set_state(queue_id, PlaybackState.PAUSED)
        self._on_player_update(queue_id)

    def _handle_play(self, queue_id: str) -> None:
        if queue_id not in self.queues:
            raise PlayerUnavailableError(f"Queue {queue_id} is not available")
        queue = self.queues[queue_id]
        with self._play_action(queue_id):
            self._get_player(queue_id, raise_unavailable=True)
            if queue.active and queue.state == PlaybackState.PAUSED:
                self._device_set_state(queue_id, PlaybackState.PLAYING)
                return
            self._resume(queue_id)

    def _resume(self, queue_id: str) -> None:
        if queue_id not in self.queues:
            raise PlayerUnavailableError(f"Queue {queue_id} is not available")
        queue = self.queues[queue_id]
        with self._play_action(queue_id):
            items = self.queue_items[queue_id]
            resume_item = queue.current_item
            if resume_item and self._index_by_id(queue_id, resume_item.queue_item_id) is None:
                resume_item = None
            if queue.state == PlaybackState.PLAYING:
                resume_pos = queue.corrected_elapsed_time
            else:
                resume_pos = queue.resume_pos or queue.elapsed_time
            if queue.ended and items:
                resume_item = items[0]
                resume_pos = 0
            elif not resume_item and items:
                resume_item = self._get_item(queue_id, queue.current_index) or items[0]
                resume_pos = 0
            if resume_item is None:
                raise QueueEmpty(f"Resume queue requested but queue {queue.display_name} is empty")
            self._get_player(queue_id, raise_unavailable=True)
            if resume_item.media_type == MediaType.RADIO:
                resume_pos = 0
            self._play_index(queue_id, resume_item.queue_item_id, int(resume_pos))

    def _next(self, queue_id: str) -> None:
        queue = self.queues.get(queue_id)
        if queue is None:
            raise InvalidCommand(f"Queue {queue_id} is not active")
        target: Optional[int] = None
        with self._play_action(queue_id):
            if not queue.active:
                raise InvalidCommand(f"Queue {queue_id} is not active")
            self._transitioning[queue_id] = True
            index = queue.current_index
            next_index = (
                self._get_next_index(queue_id, index, is_skip=True) if index is not None else None
            )
            if next_index is None:
                self._transitioning[queue_id] = False
                return
            queue.current_index = next_index
            queue.current_item = self._get_item(queue_id, next_index)
            queue.elapsed_time = 0.0
            queue.elapsed_time_last_updated = time.time()
            self._signal(queue_id)
            target = next_index
        # MA debounces this play_index by 1 s; the fake runs it straight away
        with self._play_action(queue_id):
            self._play_index(queue_id, target)

    def _previous(self, queue_id: str) -> None:
        queue = self.queues.get(queue_id)
        if queue is None:
            raise InvalidCommand(f"Queue {queue_id} is not active")
        target: Optional[int] = None
        with self._play_action(queue_id):
            if not queue.active:
                raise InvalidCommand(f"Queue {queue_id} is not active")
            self._transitioning[queue_id] = True
            current_index = queue.current_index
            if current_index is None:
                self._transitioning[queue_id] = False
                return
            prev_index = int(current_index)
            # MA 2.10.7 compares the raw (not extrapolated) elapsed_time
            if queue.elapsed_time < 5:
                prev_index = max(current_index - 1, 0)
            queue.current_index = prev_index
            queue.current_item = self._get_item(queue_id, prev_index)
            queue.elapsed_time = 0.0
            queue.elapsed_time_last_updated = time.time()
            self._signal(queue_id)
            target = prev_index
        with self._play_action(queue_id):
            self._play_index(queue_id, target)

    def _seek(self, queue_id: str, position: Union[int, float]) -> None:
        queue = self.queues.get(queue_id)
        if queue is None or not queue.active:
            raise InvalidCommand(f"Queue {queue_id} is not active")
        player = self._get_player(queue_id, raise_unavailable=True)
        assert player is not None
        if not queue.current_item:
            raise InvalidCommand(f"Queue {player.name} has no item(s) loaded.")
        if not queue.current_item.duration:
            raise InvalidCommand("Can not seek items without duration.")
        position = max(0, int(position))
        if position > queue.current_item.duration:
            raise InvalidCommand("Can not seek outside of duration range.")
        if queue.current_index is None:
            raise InvalidCommand(f"Queue {player.name} has no current index.")
        queue.elapsed_time = float(position)
        queue.elapsed_time_last_updated = time.time()
        self._signal(queue_id)
        with self._play_action(queue_id):
            self._play_index(queue_id, queue.current_index, seek_position=position)

    def _get_player(self, player_id: str, raise_unavailable: bool = False) -> Optional[Player]:
        player = self.players.get(player_id)
        if player is None or not player.available or not player.enabled:
            if raise_unavailable:
                raise PlayerUnavailableError(f"Player {player_id} is not available")
        return player

    def _device_play(self, player_id: str, item: QueueItem, seek_position: float) -> None:
        player = self.players[player_id]
        player.playback_state = (
            PlaybackState.IDLE if player_id in self.dead_player_ids else PlaybackState.PLAYING
        )
        player.active_source = player_id
        player.powered = True
        player.elapsed_time = float(seek_position)
        player.elapsed_time_last_updated = time.time()
        self._device_item[player_id] = item.queue_item_id
        self._device_media[player_id] = None
        self._touch(player_id)

    def _device_set_state(self, player_id: str, state: PlaybackState) -> None:
        player = self.players[player_id]
        now = time.time()
        if player.playback_state == PlaybackState.PLAYING:
            player.elapsed_time = float(player.corrected_elapsed_time or 0.0)
        player.elapsed_time_last_updated = now
        player.playback_state = state
        self._touch(player_id)

    def _set_volume(self, player_id: str, level: int) -> None:
        player = self.players[player_id]
        level = max(0, min(100, int(level)))
        if player.type == PlayerType.GROUP:
            self._set_group_volume(player, level)
            return
        player.volume_level = level
        player.group_volume = level
        self._touch(player_id)
        for group in self.players.values():
            if group.player_id != player_id and player_id in group.group_members:
                levels = [
                    self.players[m].volume_level or 0
                    for m in group.group_members
                    if m in self.players
                ]
                if levels:
                    group.group_volume = round(sum(levels) / len(levels))
                    self._touch(group.player_id)

    def _set_group_volume(self, group: Player, level: int) -> None:
        group.group_volume = level
        group.volume_level = level
        self._touch(group.player_id)
        for member_id in group.group_members:
            member = self.players.get(member_id)
            if member is not None and member_id != group.player_id:
                member.volume_level = level
                member.group_volume = level
                self._touch(member_id)

    def _new_item_id(self) -> str:
        self._item_seq += 1
        return f"qi{self._item_seq:04d}"

    def _build_queue_item(self, queue_id: str, media_item: MediaItem) -> QueueItem:
        item = QueueItem.from_media_item(queue_id, copy.deepcopy(media_item))  # type: ignore[arg-type]
        item.queue_item_id = self._new_item_id()
        return item

    def _resolve_media(self, entry: Any) -> MediaItem:
        if isinstance(entry, str):
            return self._resolve_uri(entry)
        media_item = media_from_dict(entry) if isinstance(entry, dict) else entry
        if isinstance(media_item, ItemMapping):
            if media_item.uri is None:
                raise InvalidDataError("ItemMapping has no URI")
            return self._resolve_uri(media_item.uri)
        if not isinstance(media_item, MediaItem):
            raise InvalidDataError(f"Can not play {entry!r}")
        return media_item

    def _resolve_uri(self, uri: str) -> MediaItem:
        provider, media_type, item_id = _parse_uri(uri)
        if media_type != "track":
            raise MediaNotFoundError(f"Item {uri} not found")
        instance_id, domain = self._resolve_provider(provider)
        return self._make_track(instance_id, domain, item_id)

    def _resolve_provider(self, key: str) -> tuple[str, str]:
        for provider in self.providers:
            if provider.instance_id == key:
                if provider.available:
                    return provider.instance_id, provider.domain
                key = provider.domain
                break
        for provider in self.providers:
            if provider.domain == key and provider.available:
                return provider.instance_id, provider.domain
        domain = key.split("--", 1)[0]
        if domain in STRICT_PROVIDER_DOMAINS or any(p.domain == domain for p in self.providers):
            raise ProviderUnavailableError(f"Provider {key} is not available")
        return key, domain

    def _make_track(self, instance_id: str, domain: str, track_id: str) -> Track:
        title = f"Track {track_id}"
        duration = DEFAULT_TRACK_DURATION
        mapping = ProviderMapping(
            item_id=track_id,
            provider_domain=domain,
            provider_instance=instance_id,
            available=track_id not in self.unplayable_track_ids,
        )
        if track_id in self.library_track_ids:
            if track_id not in self._library_ids:
                self._library_ids[track_id] = str(len(self._library_ids) + 1)
            item_id, provider = self._library_ids[track_id], "library"
        else:
            item_id, provider = track_id, instance_id
        return Track(
            item_id=item_id,
            provider=provider,
            name=title,
            duration=duration,
            provider_mappings={mapping},
            artists=UniqueList(
                [
                    ItemMapping(
                        item_id=f"artist-{track_id}",
                        provider=provider,
                        name="Test Artist",
                        media_type=MediaType.ARTIST,
                    )
                ]
            ),
            album=ItemMapping(
                item_id=f"album-{track_id}",
                provider=provider,
                name="Test Album",
                media_type=MediaType.ALBUM,
            ),
        )

    def _build_command_table(self) -> dict[str, Callable[..., Any]]:
        return {
            # initial state
            "providers": lambda sock: list(self.providers),
            "providers/manifests": lambda sock: [],
            "dashboard/dashboards": lambda sock: [],
            "dashboard/sessions": lambda sock: [],
            # players
            "players/all": self._cmd_players_all,
            "players/cmd/group_volume": self._cmd_group_volume,
            # queues
            "player_queues/all": lambda sock: list(self.queues.values()),
            "player_queues/items": self._cmd_queue_items,
            "player_queues/play_media": self._cmd_play_media,
            "player_queues/play_index": self._cmd_play_index,
            "player_queues/play": lambda sock, queue_id: self._handle_play(queue_id),
            "player_queues/pause": lambda sock, queue_id: self._pause(queue_id),
            "player_queues/stop": lambda sock, queue_id: self._handle_stop(queue_id),
            "player_queues/seek": self._cmd_seek,
            "player_queues/clear": self._cmd_clear,
            "player_queues/delete_item": self._cmd_delete_item,
            "player_queues/repeat": self._cmd_repeat,
            "player_queues/autoplay": self._cmd_autoplay,
            # auth
            "auth/token/create": self._cmd_token_create,
            "auth/logout": self._cmd_logout,
        }

    # players ---------------------------------------------------------------------------

    def _cmd_players_all(self, sock: _ServerSocket) -> list[Player]:
        result = []
        for player_id, player in self.players.items():
            if not player.enabled or player.type == PlayerType.PROTOCOL:
                continue
            self._refresh_player(player_id)
            result.append(player)
        return result

    def _cmd_volume_set(self, sock: _ServerSocket, player_id: str, volume_level: int) -> None:
        player = self._get_player(player_id, raise_unavailable=True)
        assert player is not None
        if player.type != PlayerType.GROUP and (
            PlayerFeature.VOLUME_SET not in player.supported_features
        ):
            raise UnsupportedFeaturedException(f"Player {player.name} does not support volume_set")
        self._set_volume(player_id, volume_level)

    def _cmd_group_volume(self, sock: _ServerSocket, player_id: str, volume_level: int) -> None:
        player = self._get_player(player_id, raise_unavailable=True)
        assert player is not None
        if player.type == PlayerType.GROUP or len(player.group_members) > 1:
            self._set_group_volume(player, max(0, min(100, int(volume_level))))
            return
        # not grouped: MA treats it as a normal volume change
        self._cmd_volume_set(sock, player_id, volume_level)

    # queues ----------------------------------------------------------------------------

    def _cmd_queue_items(
        self, sock: _ServerSocket, queue_id: str, limit: int = 500, offset: int = 0
    ) -> list[QueueItem]:
        items = self.queue_items.get(queue_id)
        if items is None:
            return []
        return items[int(offset) : int(offset) + int(limit)]

    def _cmd_play_media(
        self,
        sock: _ServerSocket,
        queue_id: str,
        media: Any,
        option: Optional[str] = None,
        radio_mode: bool = False,
        start_item: Optional[str] = None,
        sort_by: Optional[str] = None,
        start_from_beginning: bool = False,
        shuffle: Optional[bool] = None,
    ) -> None:
        self._handle_play_media(queue_id, media, option)

    def _cmd_play_index(
        self,
        sock: _ServerSocket,
        queue_id: str,
        index: Union[int, str],
        seek_position: int = 0,
        fade_in: bool = False,
    ) -> None:
        if queue_id not in self.queues:
            raise PlayerUnavailableError(f"Queue {queue_id} is not available")
        with self._play_action(queue_id):
            self._play_index(queue_id, index, seek_position)

    def _cmd_seek(self, sock: _ServerSocket, queue_id: str, position: int = 10) -> None:
        self._seek(queue_id, position)

    def _cmd_clear(self, sock: _ServerSocket, queue_id: str, skip_stop: bool = False) -> None:
        if queue_id not in self.queues:
            raise PlayerUnavailableError(f"Queue {queue_id} is not available")
        self._clear(queue_id, bool(skip_stop))

    def _cmd_delete_item(
        self, sock: _ServerSocket, queue_id: str, item_id_or_index: Union[int, str]
    ) -> None:
        if queue_id not in self.queues:
            raise PlayerUnavailableError(f"Queue {queue_id} is not available")
        if isinstance(item_id_or_index, str):
            index = self._index_by_id(queue_id, item_id_or_index)
            if index is None:
                raise InvalidDataError(f"Item {item_id_or_index} not found in queue")
        else:
            index = int(item_id_or_index)
        queue = self.queues[queue_id]
        boundary = committed_index(queue) if queue.index_in_buffer is not None else None
        if boundary is not None and index <= boundary:
            # MA: "delete requested for item already loaded in buffer" -> silently ignored
            logger.debug("delete requested for item already loaded in buffer")
            return
        items = list(self.queue_items[queue_id])
        items.pop(index)
        self._update_items(queue_id, items)

    def _cmd_repeat(self, sock: _ServerSocket, queue_id: str, repeat_mode: str) -> None:
        queue = self.queues[queue_id]
        mode = RepeatMode(repeat_mode)
        if queue.repeat_mode == mode:
            return
        if queue.is_dynamic:
            raise InvalidCommand("Cannot change repeat while the queue is in dynamic mode")
        queue.repeat_mode = mode
        self._signal(queue_id)

    def _cmd_autoplay(self, sock: _ServerSocket, queue_id: str, autoplay_enabled: bool) -> None:
        queue = self.queues[queue_id]
        queue.autoplay_enabled = bool(autoplay_enabled)
        self._signal(queue_id)

    # auth ------------------------------------------------------------------------------

    def _cmd_token_create(
        self, sock: _ServerSocket, name: str, user_id: Optional[str] = None
    ) -> str:
        username = sock.username or "admin"
        if username in self.guest_users:
            raise InsufficientPermissions("Guest users cannot create long-lived tokens")
        self._token_seq += 1
        token = f"fake-long-lived-token-{self._token_seq}"
        self._register_token(token, username, name, long_lived=True)
        return token

    def _cmd_logout(self, sock: _ServerSocket) -> None:
        if sock.token is not None:
            asyncio.get_running_loop().call_soon(self.revoke_token, sock.token)


def _error_frame(message_id: Any, error_code: int, details: str) -> dict[str, Any]:
    return {"message_id": message_id, "error_code": error_code, "details": details}


def _exception_frame(message_id: Any, err: BaseException) -> dict[str, Any]:
    """What MA's websocket handler sends for an exception (client maps it via ERROR_MAP)."""
    code = getattr(err, "error_code", 999)
    return _error_frame(message_id, code, str(err) or err.__class__.__name__)


def _parse_uri(uri: str) -> tuple[str, str, str]:
    """``(provider, media_type, item_id)`` like MA's ``parse_uri`` (InvalidProviderURI)."""
    if "://" not in uri:
        raise InvalidProviderURI(f"Not a valid Music Assistant uri: {uri}")
    provider, rest = uri.split("://", 1)
    parts = rest.split("/", 1)
    if not provider or len(parts) != 2 or not parts[0] or not parts[1]:
        raise InvalidProviderURI(f"Not a valid Music Assistant uri: {uri}")
    return provider, parts[0], parts[1]


def _item_track_id(item: QueueItem) -> Optional[str]:
    """Provider track id of a queue item (library items: from the provider mapping)."""
    media_item = item.media_item
    if media_item is None:
        return None
    if media_item.provider == "library":
        for mapping in getattr(media_item, "provider_mappings", ()) or ():
            return str(mapping.item_id)
        return None
    return str(media_item.item_id)
