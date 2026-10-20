"""
One websocket to Music Assistant, shared by every exposed player.

A ``MusicAssistantClient`` never reconnects by itself and cannot be reused
after a disconnect, so every connection attempt builds a new one.
"""

import asyncio
import contextlib
import logging
import time
from dataclasses import dataclass
from enum import Enum
from typing import Any, Awaitable, Callable, Optional, TypeVar

import aiohttp
from music_assistant_client import MusicAssistantClient, login
from music_assistant_client.exceptions import (
    CannotConnect,
    InvalidServerVersion,
    MusicAssistantClientException,
)
from music_assistant_models.enums import PlayerType
from music_assistant_models.errors import (
    AuthenticationFailed,
    AuthenticationRequired,
    InsufficientPermissions,
    InvalidToken,
    LoginFailed,
    MusicAssistantError,
)

from . import LoginError

logger = logging.getLogger(__name__)
# The client's debug log prints every websocket message, the auth token included
logging.getLogger("music_assistant_client").setLevel(logging.INFO)

T = TypeVar("T")

PLAYER_TYPES = {
    PlayerType.PLAYER: "player",
    PlayerType.STEREO_PAIR: "stereo_pair",
    PlayerType.GROUP: "group",
}
TOKEN_NAME = "Qobuz Bridge"
RECONNECT_DELAYS_SECONDS = (1.0, 2.0, 5.0, 10.0, 20.0, 30.0, 60.0)
CONNECT_TIMEOUT_SECONDS = 15.0
LOAD_TIMEOUT_SECONDS = 30.0
CALL_TIMEOUT_SECONDS = 10.0
# A connection that drops sooner than this does not reset the reconnect backoff
STABLE_CONNECTION_SECONDS = 30.0

ClientFactory = Callable[[str, aiohttp.ClientSession, str], Any]
EventListener = Callable[[Any], None]
StatusListener = Callable[["ConnectionStatus"], None]


class ConnectionState(str, Enum):
    NOT_CONFIGURED = "not_configured"
    CONNECTING = "connecting"
    CONNECTED = "connected"
    DISCONNECTED = "disconnected"
    AUTH_FAILED = "auth_failed"


@dataclass(frozen=True)
class ConnectionStatus:
    state: ConnectionState
    message: str = ""  # plain language, shown in the web UI
    server_version: str = ""
    has_qobuz_provider: bool = False  # MA has a working Qobuz provider


@dataclass(frozen=True)
class PlayerInfo:
    """An enabled MA player, stereo pair or group."""

    player_id: str
    name: str
    type: str  # a PLAYER_TYPES value
    available: bool
    hidden: bool  # hidden in MA's own UI


class NotConnectedError(RuntimeError):
    """Music Assistant is not connected, or the connection dropped mid-call."""


def _unreachable_message(url: str) -> str:
    return f"Can't reach Music Assistant at {url}. Is the Music Assistant add-on running?"


def _token_rejected(err: Exception) -> bool:
    """Whether MA turned down the token itself (expired, revoked, user removed)."""
    return (
        isinstance(err, AuthenticationRequired)
        or isinstance(err.__cause__, InvalidToken)
        or "invalid or expired token" in str(err).lower()
    )


class MusicAssistantConnection:
    """Keeps one websocket to Music Assistant open and shares it."""

    def __init__(
        self, url: str = "", token: str = "", client_factory: ClientFactory = MusicAssistantClient
    ) -> None:
        self._url = url
        self._token = token
        self._client_factory = client_factory
        self._http: Optional[aiohttp.ClientSession] = None
        self._client: Optional[Any] = None
        self._task: Optional[asyncio.Task[None]] = None
        self._status = ConnectionStatus(ConnectionState.NOT_CONFIGURED)
        self._event_listeners: dict[Optional[str], list[EventListener]] = {}
        self._status_listeners: list[StatusListener] = []
        self._timeouts_in_a_row = 0
        self._background: set[asyncio.Task[Any]] = set()

    @property
    def status(self) -> ConnectionStatus:
        return self._status

    @property
    def is_connected(self) -> bool:
        return self._client is not None

    async def start(self) -> None:
        """Start connecting in the background; without an address and token, do nothing."""
        if self._task is not None and not self._task.done():
            return
        if not (self._url and self._token):
            self._set_status(ConnectionState.NOT_CONFIGURED)
            return
        self._set_status(ConnectionState.CONNECTING, "Connecting to Music Assistant…")
        self._task = asyncio.create_task(self._keep_connected(), name="music-assistant-connection")

    async def stop(self) -> None:
        """Disconnect and stop reconnecting."""
        task, self._task = self._task, None
        if task is not None:
            task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await task
        if self._http is not None:
            await self._http.close()
            self._http = None

    async def reconnect(self, url: str, token: str) -> None:
        """Switch to another address or token."""
        await self.stop()
        self._url, self._token = url, token
        await self.start()

    def add_event_listener(self, object_id: Optional[str], listener: EventListener) -> None:
        """Call ``listener(event)`` for MA events about ``object_id`` (None: all events).

        Listeners run after the client updated its player and queue caches.
        """
        self._event_listeners.setdefault(object_id, []).append(listener)

    def remove_event_listener(self, object_id: Optional[str], listener: EventListener) -> None:
        self._event_listeners.get(object_id, []).remove(listener)

    def add_status_listener(self, listener: StatusListener) -> None:
        self._status_listeners.append(listener)

    def remove_status_listener(self, listener: StatusListener) -> None:
        self._status_listeners.remove(listener)

    def players(self) -> list[PlayerInfo]:
        if self._client is None:
            return []
        return [
            PlayerInfo(
                player_id=p.player_id,
                name=p.name or p.player_id,
                type=PLAYER_TYPES[p.type],
                available=p.available,
                hidden=p.hide_in_ui,
            )
            for p in self._client.players
            if p.type in PLAYER_TYPES and p.enabled
        ]

    def player(self, player_id: str) -> Optional[Any]:
        return self._client.players.get(player_id) if self._client is not None else None

    def queue(self, queue_id: str) -> Optional[Any]:
        return self._client.player_queues.get(queue_id) if self._client is not None else None

    async def call(
        self,
        description: str,
        command: Callable[[Any], Awaitable[T]],
        timeout: float = CALL_TIMEOUT_SECONDS,
    ) -> T:
        """Run one MA command and log it.

        Raises:
            NotConnectedError: MA is not connected, did not answer in time, or
                the socket dropped mid-call.
            MusicAssistantError: MA answered with an error.
        """
        client = self._client
        if client is None:
            logger.warning(
                f"Music Assistant did not do {description}: not connected to Music Assistant"
            )
            raise NotConnectedError("Music Assistant is not connected")
        logger.info(f"To Music Assistant: {description}")
        try:
            result = await asyncio.wait_for(command(client), timeout)
        except asyncio.TimeoutError:
            logger.warning(
                f"Music Assistant did not do {description}: no answer from Music Assistant in {timeout:.0f}s"
            )
            self._timeouts_in_a_row += 1
            stuck = self._timeouts_in_a_row >= 2 and self._client is client
            if stuck:
                # Probably a dead socket the client has not noticed yet
                logger.warning("Music Assistant stopped answering; reconnecting")
                self._run_in_background(client.disconnect())
            raise NotConnectedError("Music Assistant did not answer in time") from None
        except asyncio.CancelledError:
            # The client cancels pending commands when its socket closes
            task = asyncio.current_task()
            if task is not None and task.cancelling() == 0:  # type: ignore[attr-defined]
                logger.warning(
                    f"Music Assistant did not do {description}: connection to Music Assistant lost"
                )
                raise NotConnectedError("Lost the connection to Music Assistant") from None
            raise
        except (MusicAssistantClientException, aiohttp.ClientError, ConnectionError) as e:
            logger.warning(
                f"Music Assistant did not do {description}: connection to Music Assistant lost ({e})"
            )
            raise NotConnectedError("Lost the connection to Music Assistant") from e
        except MusicAssistantError as e:
            self._timeouts_in_a_row = 0
            logger.warning(f"Music Assistant did not do {description}: {type(e).__name__}: {e}")
            raise
        self._timeouts_in_a_row = 0
        return result

    def _set_status(
        self,
        state: ConnectionState,
        message: str = "",
        server_version: str = "",
        has_qobuz_provider: bool = False,
    ) -> None:
        status = ConnectionStatus(state, message, server_version, has_qobuz_provider)
        if status == self._status:
            return
        self._status = status
        for listener in list(self._status_listeners):
            try:
                listener(status)
            except Exception:
                logger.exception("Music Assistant status listener failed")

    def _dispatch(self, event: Any) -> None:
        """Hand one MA event to its listeners (a failing one must not end the client's loop)."""
        listeners = self._event_listeners.get(None, []) + self._event_listeners.get(
            event.object_id, []
        )
        for listener in listeners:
            try:
                listener(event)
            except Exception:
                logger.exception("Music Assistant event listener failed")

    def _run_in_background(self, coro: Awaitable[Any]) -> None:
        task = asyncio.ensure_future(coro)
        self._background.add(task)
        task.add_done_callback(self._background.discard)

    async def _keep_connected(self) -> None:
        """Connect, and reconnect with backoff, until stopped or the token is rejected."""
        attempt = 0
        while True:
            try:
                outcome = await self._connect_and_listen()
            except Exception:
                logger.exception("Unexpected error in the Music Assistant connection")
                self._set_status(ConnectionState.DISCONNECTED, _unreachable_message(self._url))
                outcome = False
            if outcome is None:
                return
            if outcome:
                attempt = 0  # a healthy connection dropped (e.g. MA restarted): retry soon
            delay = RECONNECT_DELAYS_SECONDS[min(attempt, len(RECONNECT_DELAYS_SECONDS) - 1)]
            attempt += 1
            logger.info(f"Reconnecting to Music Assistant in {delay:.0f}s")
            await asyncio.sleep(delay)

    async def _connect_and_listen(self) -> Optional[bool]:
        """One connection: True if it was healthy until it dropped, False if not, None to stop."""
        if self._http is None:
            self._http = aiohttp.ClientSession()
        self._set_status(ConnectionState.CONNECTING, "Connecting to Music Assistant…")
        client = self._client_factory(self._url, self._http, self._token)
        listen: Optional[asyncio.Task[None]] = None
        try:
            try:
                await asyncio.wait_for(client.connect(), CONNECT_TIMEOUT_SECONDS)
            except (AuthenticationFailed, AuthenticationRequired) as e:
                if _token_rejected(e):
                    logger.error("Music Assistant rejected the saved login; log in again")
                    self._set_status(
                        ConnectionState.AUTH_FAILED,
                        "Music Assistant no longer accepts the saved login (it may have expired "
                        "or been removed). Connect again in this panel.",
                    )
                    return None
                logger.warning(f"Music Assistant refused the connection: {e}")
                self._set_status(
                    ConnectionState.DISCONNECTED, f"Music Assistant refused the connection ({e})."
                )
                return False
            except InvalidServerVersion as e:
                logger.error(f"Unsupported Music Assistant version: {e}")
                self._set_status(
                    ConnectionState.DISCONNECTED,
                    f"This Music Assistant version is not supported ({e}).",
                )
                return False
            except (
                asyncio.TimeoutError,
                OSError,
                MusicAssistantClientException,
                MusicAssistantError,
            ) as e:
                logger.warning(f"Can't reach Music Assistant at {self._url}: {e}")
                self._set_status(ConnectionState.DISCONNECTED, _unreachable_message(self._url))
                return False

            client.subscribe(self._dispatch)
            loaded = asyncio.Event()
            listen = asyncio.create_task(client.start_listening(loaded))
            wait_loaded = asyncio.create_task(loaded.wait())
            await asyncio.wait(
                {listen, wait_loaded},
                timeout=LOAD_TIMEOUT_SECONDS,
                return_when=asyncio.FIRST_COMPLETED,
            )
            wait_loaded.cancel()
            if not loaded.is_set():
                logger.warning("Music Assistant did not send its players and queues")
                self._set_status(ConnectionState.DISCONNECTED, _unreachable_message(self._url))
                return False

            self._client = client
            connected_at = time.monotonic()
            self._timeouts_in_a_row = 0
            version = client.server_info.server_version
            has_qobuz_provider = any(p.domain == "qobuz" and p.available for p in client.providers)
            self._set_status(
                ConnectionState.CONNECTED,
                f"Connected to Music Assistant {version}.",
                version,
                has_qobuz_provider,
            )
            logger.info(
                f"Connected to Music Assistant {version} at {self._url} "
                f"({len(self.players())} players, "
                f"{'Qobuz provider ready' if has_qobuz_provider else 'no Qobuz provider'})"
            )
            try:
                await listen
                logger.warning("Music Assistant closed the connection")
            except (MusicAssistantClientException, OSError) as e:
                logger.warning(f"Connection to Music Assistant failed: {e}")
            self._client = None
            self._set_status(
                ConnectionState.DISCONNECTED,
                "Lost the connection to Music Assistant; reconnecting…",
            )
            return time.monotonic() - connected_at >= STABLE_CONNECTION_SECONDS
        finally:
            self._client = None
            if listen is not None and not listen.done():
                listen.cancel()
                with contextlib.suppress(asyncio.CancelledError, Exception):
                    await listen
            with contextlib.suppress(Exception):
                await client.disconnect()


async def login_and_create_token(url: str, username: str, password: str) -> tuple[str, str]:
    """Log in with username and password and create a long-lived token.

    Returns:
        (long-lived token, the user's display name)

    Raises:
        LoginError: with a plain-language message.
    """
    unreachable = (
        f"Can't reach Music Assistant at {url}. Check the address and that the Music "
        "Assistant add-on is running."
    )
    async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=15)) as http:
        try:
            user, session_token = await login(url, username, password, http)
        except LoginFailed:
            raise LoginError(
                "Music Assistant did not accept that username and password. If you only ever "
                "open Music Assistant from the Home Assistant sidebar you may not have a Music "
                "Assistant password yet: set one in Music Assistant (your user profile)."
            ) from None
        except CannotConnect as e:
            if "403" in str(e):
                raise LoginError(
                    "Music Assistant is not set up yet. Open it once, finish the setup, then try "
                    "again."
                ) from None
            raise LoginError(unreachable) from None
        except (asyncio.TimeoutError, OSError):
            raise LoginError(unreachable) from None

        client = MusicAssistantClient(url, http, token=session_token)
        try:
            await asyncio.wait_for(client.connect(), CONNECT_TIMEOUT_SECONDS)
            try:
                token = await asyncio.wait_for(
                    client.auth.create_token(TOKEN_NAME), CALL_TIMEOUT_SECONDS
                )
            finally:
                # The login also created a 30-day session token that is no longer needed
                with contextlib.suppress(Exception):
                    await asyncio.wait_for(client.auth.logout(), CALL_TIMEOUT_SECONDS)
        except InsufficientPermissions:
            raise LoginError(
                "This Music Assistant user is a guest and can't create the long-lived login the "
                "bridge needs. Use a regular user."
            ) from None
        except (asyncio.TimeoutError, OSError, MusicAssistantClientException):
            raise LoginError(unreachable) from None
        except MusicAssistantError as e:
            raise LoginError(f"Music Assistant could not create a login token ({e}).") from None
        finally:
            with contextlib.suppress(Exception):
                await client.disconnect()
    name = user.display_name or user.username or username
    logger.info(f"Created a long-lived Music Assistant token for user {name}")
    return str(token), name
