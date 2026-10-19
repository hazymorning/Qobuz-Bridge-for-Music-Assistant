"""
Shared connection to a Music Assistant server.

One :class:`MusicAssistantConnection` serves every exposed MA player. It keeps a
websocket to MA open with the official ``music-assistant-client``, reconnects
with backoff when MA restarts or is unreachable, stops retrying when MA no
longer accepts the saved token, and fans MA events out to the backends.

The client never reconnects by itself and an instance cannot be reused after a
disconnect, so every attempt builds a new client.
"""

import asyncio
import logging
import time
from dataclasses import dataclass
from enum import Enum
from typing import Any, Awaitable, Callable, Optional, TypeVar

import aiohttp
from music_assistant_client import MusicAssistantClient, login
from music_assistant_client.exceptions import (
    CannotConnect,
    ConnectionClosed,
    ConnectionFailed,
    InvalidServerVersion,
    InvalidState,
    MusicAssistantClientException,
    NotConnected,
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

logger = logging.getLogger(__name__)

T = TypeVar("T")

# What a Qobuz Connect device can stand for: real players, stereo pairs, groups.
EXPOSABLE_PLAYER_TYPES = {
    PlayerType.PLAYER: "player",
    PlayerType.STEREO_PAIR: "stereo_pair",
    PlayerType.GROUP: "group",
}

TOKEN_NAME = "Qobuz Bridge"

ClientFactory = Callable[[str, aiohttp.ClientSession, str], Any]
EventListener = Callable[[Any], None]


class MAConnectionState(str, Enum):
    """Connection state, as shown in the web UI."""

    NOT_CONFIGURED = "not_configured"
    CONNECTING = "connecting"
    CONNECTED = "connected"
    DISCONNECTED = "disconnected"
    AUTH_FAILED = "auth_failed"


@dataclass
class MAConnectionStatus:
    """Snapshot of the connection for the UI and the backends."""

    state: MAConnectionState
    url: str = ""
    message: str = ""  # plain language, safe to show
    server_version: str = ""
    qobuz_provider: Optional[bool] = None  # None = unknown (not connected)
    changed_at: float = 0.0  # time.monotonic() of the last state change


@dataclass(frozen=True)
class MAPlayerInfo:
    """An MA player or group that could be exposed as a Qobuz Connect device."""

    player_id: str
    name: str
    player_type: str  # "player" | "group" | "stereo_pair"
    available: bool
    enabled: bool
    hidden: bool  # hidden in MA's UI
    provider: str


class MANotConnectedError(RuntimeError):
    """Music Assistant is not connected, or the connection dropped mid-call."""


class MALoginError(Exception):
    """A login or token problem; the message is plain language for the UI."""


def _unreachable_message(url: str) -> str:
    return f"Can't reach Music Assistant at {url}. Is the Music Assistant add-on running?"


def _is_invalid_token(err: BaseException) -> bool:
    """Whether MA rejected the token itself (expired, revoked, user removed)."""
    cause = err.__cause__
    return (
        isinstance(err, InvalidToken)
        or isinstance(cause, InvalidToken)
        or ("invalid or expired token" in str(err).lower())
    )


def _default_client_factory(url: str, session: aiohttp.ClientSession, token: str) -> Any:
    return MusicAssistantClient(url, session, token=token)


class MusicAssistantConnection:
    """Keeps one websocket to Music Assistant and shares it between backends."""

    RECONNECT_DELAYS_SECONDS = (1.0, 2.0, 5.0, 10.0, 20.0, 30.0, 60.0)
    CONNECT_TIMEOUT_SECONDS = 15.0
    INIT_TIMEOUT_SECONDS = 30.0

    def __init__(
        self,
        url: str = "",
        token: str = "",
        *,
        client_factory: Optional[ClientFactory] = None,
    ) -> None:
        self._url = url.rstrip("/")
        self._token = token
        self._client_factory = client_factory or _default_client_factory
        self._session: Optional[aiohttp.ClientSession] = None
        self._client: Optional[Any] = None
        self._task: Optional[asyncio.Task[None]] = None
        self._status = MAConnectionStatus(
            state=MAConnectionState.NOT_CONFIGURED
            if not (url and token)
            else MAConnectionState.DISCONNECTED,
            url=self._url,
            message="" if (url and token) else "Not connected to Music Assistant yet.",
            changed_at=time.monotonic(),
        )
        self._event_listeners: dict[Optional[str], list[EventListener]] = {}
        self._state_listeners: list[Callable[[MAConnectionStatus], None]] = []
        self._timeouts = 0  # consecutive command timeouts

    # ------------------------------------------------------------------
    # Properties
    # ------------------------------------------------------------------

    @property
    def url(self) -> str:
        return self._url

    @property
    def status(self) -> MAConnectionStatus:
        return self._status

    @property
    def is_connected(self) -> bool:
        return self._status.state == MAConnectionState.CONNECTED and self._client is not None

    @property
    def client(self) -> Optional[Any]:
        """The live client, only while connected."""
        return self._client if self.is_connected else None

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    async def start(self) -> None:
        """Start connecting in the background (no-op without an address and token)."""
        if self._task and not self._task.done():
            return
        if not (self._url and self._token):
            self._set_state(
                MAConnectionState.NOT_CONFIGURED, "Not connected to Music Assistant yet."
            )
            return
        self._task = asyncio.create_task(self._run(), name="music-assistant-connection")

    async def stop(self) -> None:
        """Disconnect and stop reconnecting. Safe to call more than once."""
        task, self._task = self._task, None
        if task and not task.done():
            task.cancel()
            try:
                await task
            except (asyncio.CancelledError, Exception):
                pass
        await self._drop_client()
        if self._session is not None:
            session, self._session = self._session, None
            await session.close()

    async def reconfigure(self, url: str, token: str) -> None:
        """Switch to a new address/token and reconnect."""
        await self.stop()
        self._url = url.rstrip("/")
        self._token = token
        self._status.url = self._url
        await self.start()

    # ------------------------------------------------------------------
    # Listeners
    # ------------------------------------------------------------------

    def add_event_listener(
        self, object_id: Optional[str], callback: EventListener
    ) -> Callable[[], None]:
        """Call ``callback(event)`` for MA events about ``object_id`` (None = all).

        Callbacks run after the client updated its player/queue caches, so they
        can read the new state from :meth:`get_player` / :meth:`get_queue`.
        """
        self._event_listeners.setdefault(object_id, []).append(callback)

        def remove() -> None:
            listeners = self._event_listeners.get(object_id, [])
            if callback in listeners:
                listeners.remove(callback)

        return remove

    def add_state_listener(
        self, callback: Callable[[MAConnectionStatus], None]
    ) -> Callable[[], None]:
        """Call ``callback(status)`` whenever the connection state changes."""
        self._state_listeners.append(callback)

        def remove() -> None:
            if callback in self._state_listeners:
                self._state_listeners.remove(callback)

        return remove

    # ------------------------------------------------------------------
    # State access (reads the client's caches, never calls MA)
    # ------------------------------------------------------------------

    def list_players(self) -> list[MAPlayerInfo]:
        """Players, stereo pairs and groups MA currently knows about."""
        client = self.client
        if client is None:
            return []
        players = []
        for player in client.players:
            player_type = EXPOSABLE_PLAYER_TYPES.get(player.type)
            if player_type is None:
                continue
            players.append(
                MAPlayerInfo(
                    player_id=player.player_id,
                    name=player.name or player.player_id,
                    player_type=player_type,
                    available=bool(player.available),
                    enabled=bool(getattr(player, "enabled", True)),
                    hidden=bool(getattr(player, "hide_in_ui", False)),
                    provider=str(getattr(player, "provider", "")),
                )
            )
        return players

    def get_player(self, player_id: str) -> Optional[Any]:
        client = self.client
        return client.players.get(player_id) if client is not None else None

    def get_queue(self, queue_id: str) -> Optional[Any]:
        client = self.client
        return client.player_queues.get(queue_id) if client is not None else None

    # ------------------------------------------------------------------
    # Commands
    # ------------------------------------------------------------------

    async def call(
        self,
        description: str,
        fn: Callable[[Any], Awaitable[T]],
        timeout: float = 10.0,
    ) -> T:
        """Run one MA command, logged, with connection errors mapped.

        Raises:
            MANotConnectedError: MA is not connected, did not answer within
                ``timeout``, or the socket dropped mid-call.
            MusicAssistantError: MA answered with an error (logged, re-raised).
        """
        client = self.client
        if client is None:
            logger.warning(f"MA ✗ {description}: not connected to Music Assistant")
            raise MANotConnectedError("Music Assistant is not connected")
        logger.info(f"MA → {description}")
        try:
            result = await asyncio.wait_for(fn(client), timeout)
        except asyncio.TimeoutError:
            self._timeouts += 1
            logger.warning(f"MA ✗ {description}: no answer from Music Assistant in {timeout:.0f}s")
            stuck = self._client
            if self._timeouts >= 2 and stuck is not None and stuck is client:
                # Probably a dead socket the heartbeat has not noticed yet
                logger.warning("Music Assistant stopped answering; reconnecting")
                asyncio.ensure_future(stuck.disconnect())
            raise MANotConnectedError("Music Assistant did not answer in time") from None
        except asyncio.CancelledError:
            # The client cancels pending commands when its socket closes. That is
            # a lost connection unless our own task is the one being cancelled.
            task = asyncio.current_task()
            cancelling = getattr(task, "cancelling", None)
            if task is not None and cancelling is not None and cancelling() == 0:
                logger.warning(f"MA ✗ {description}: connection to Music Assistant lost")
                raise MANotConnectedError("Lost the connection to Music Assistant") from None
            raise
        except (InvalidState, NotConnected, ConnectionClosed, ConnectionFailed) as e:
            logger.warning(f"MA ✗ {description}: connection to Music Assistant lost ({e})")
            raise MANotConnectedError("Lost the connection to Music Assistant") from e
        except MusicAssistantError as e:
            self._timeouts = 0
            logger.warning(f"MA ✗ {description}: {type(e).__name__}: {e}")
            raise
        self._timeouts = 0
        return result

    # ------------------------------------------------------------------
    # Internals
    # ------------------------------------------------------------------

    def _set_state(
        self,
        state: MAConnectionState,
        message: str = "",
        *,
        server_version: Optional[str] = None,
        qobuz_provider: Optional[bool] = None,
    ) -> None:
        changed = state != self._status.state
        self._status = MAConnectionStatus(
            state=state,
            url=self._url,
            message=message,
            server_version=(
                server_version if server_version is not None else self._status.server_version
            ),
            qobuz_provider=qobuz_provider if state == MAConnectionState.CONNECTED else None,
            changed_at=time.monotonic() if changed else self._status.changed_at,
        )
        for listener in list(self._state_listeners):
            try:
                listener(self._status)
            except Exception as e:
                logger.error(f"Music Assistant state listener error: {e}", exc_info=True)

    def _dispatch(self, event: Any) -> None:
        """Fan one MA event out to the listeners (runs in the event loop)."""
        object_id = getattr(event, "object_id", None)
        listeners = list(self._event_listeners.get(None, []))
        if object_id is not None:
            listeners += self._event_listeners.get(object_id, [])
        for listener in listeners:
            try:
                listener(event)
            except Exception as e:
                logger.error(f"Music Assistant event listener error: {e}", exc_info=True)

    def _qobuz_provider_available(self, client: Any) -> bool:
        return any(
            getattr(p, "domain", "") == "qobuz" and getattr(p, "available", False)
            for p in (client.providers or [])
        )

    async def _drop_client(self) -> None:
        client, self._client = self._client, None
        if client is not None:
            try:
                await client.disconnect()
            except Exception as e:
                logger.debug(f"Error disconnecting from Music Assistant: {e}")

    def _backoff_delay(self, attempt: int) -> float:
        delays = self.RECONNECT_DELAYS_SECONDS
        return delays[min(attempt, len(delays) - 1)]

    async def _run(self) -> None:
        """Connect, listen, and reconnect until stopped or the token is rejected."""
        attempt = 0
        while True:
            retry = await self._connect_once()
            if not retry:
                return
            delay = self._backoff_delay(attempt)
            attempt += 1
            if self._status.state == MAConnectionState.CONNECTED:
                attempt = 0
            logger.info(f"Reconnecting to Music Assistant in {delay:.0f}s")
            await asyncio.sleep(delay)

    async def _connect_once(self) -> bool:
        """One connect-and-listen cycle. Returns False when retrying is pointless."""
        if self._session is None or self._session.closed:
            self._session = aiohttp.ClientSession()
        self._set_state(MAConnectionState.CONNECTING, "Connecting to Music Assistant…")
        client = self._client_factory(self._url, self._session, self._token)
        unsubscribe: Optional[Callable[[], None]] = None
        listen: Optional[asyncio.Task[None]] = None
        try:
            try:
                await asyncio.wait_for(client.connect(), self.CONNECT_TIMEOUT_SECONDS)
            except (AuthenticationFailed, AuthenticationRequired, InvalidToken) as e:
                if _is_invalid_token(e) or isinstance(e, AuthenticationRequired):
                    logger.error(
                        "Music Assistant rejected the saved login (token expired or revoked); "
                        "connect again in the web UI"
                    )
                    self._set_state(
                        MAConnectionState.AUTH_FAILED,
                        "Music Assistant no longer accepts the saved login (it may have expired "
                        "or been removed). Connect again in this panel.",
                    )
                    return False
                logger.warning(f"Music Assistant refused the connection: {e}")
                self._set_state(
                    MAConnectionState.DISCONNECTED,
                    f"Music Assistant refused the connection ({e}).",
                )
                return True
            except InvalidServerVersion as e:
                logger.error(f"Unsupported Music Assistant version: {e}")
                self._set_state(
                    MAConnectionState.DISCONNECTED,
                    f"This Music Assistant version is not supported ({e}).",
                )
                return True
            except (
                CannotConnect,
                asyncio.TimeoutError,
                OSError,
                MusicAssistantClientException,
                MusicAssistantError,
            ) as e:
                logger.warning(f"Can't reach Music Assistant at {self._url}: {e}")
                self._set_state(MAConnectionState.DISCONNECTED, _unreachable_message(self._url))
                return True

            unsubscribe = client.subscribe(self._dispatch)
            ready = asyncio.Event()
            listen = asyncio.create_task(client.start_listening(ready))
            ready_wait = asyncio.create_task(ready.wait())
            done, _ = await asyncio.wait(
                {listen, ready_wait},
                timeout=self.INIT_TIMEOUT_SECONDS,
                return_when=asyncio.FIRST_COMPLETED,
            )
            ready_wait.cancel()
            if not ready.is_set():
                reason = "timed out loading players" if not done else "connection closed"
                logger.warning(f"Music Assistant did not finish starting the session ({reason})")
                self._set_state(MAConnectionState.DISCONNECTED, _unreachable_message(self._url))
                return True

            self._client = client
            self._timeouts = 0
            server_info = getattr(client, "server_info", None)
            version = str(getattr(server_info, "server_version", "") or "")
            qobuz = self._qobuz_provider_available(client)
            players = len(self.list_players())
            logger.info(
                f"Connected to Music Assistant {version} at {self._url} ({players} players"
                f"{', Qobuz provider ready' if qobuz else ', no Qobuz provider'})"
            )
            self._set_state(
                MAConnectionState.CONNECTED,
                f"Connected to Music Assistant {version}.".replace("  ", " "),
                server_version=version,
                qobuz_provider=qobuz,
            )
            try:
                await listen
                logger.warning("Music Assistant closed the connection")
            except (ConnectionFailed, MusicAssistantClientException, OSError) as e:
                logger.warning(f"Connection to Music Assistant failed: {e}")
            self._client = None
            self._set_state(
                MAConnectionState.DISCONNECTED,
                "Lost the connection to Music Assistant; reconnecting…",
            )
            return True
        except asyncio.CancelledError:
            raise
        except Exception as e:  # never let the reconnect loop die
            logger.error(f"Unexpected Music Assistant connection error: {e}", exc_info=True)
            self._set_state(MAConnectionState.DISCONNECTED, _unreachable_message(self._url))
            return True
        finally:
            if listen is not None and not listen.done():
                listen.cancel()
                try:
                    await listen
                except (asyncio.CancelledError, Exception):
                    pass
            if unsubscribe is not None:
                try:
                    unsubscribe()
                except ValueError:
                    pass
            if self._client is client:
                self._client = None
            try:
                await client.disconnect()
            except Exception:
                pass


async def login_and_create_token(
    url: str,
    username: str,
    password: str,
    *,
    token_name: str = TOKEN_NAME,
    session: Optional[aiohttp.ClientSession] = None,
) -> tuple[str, str]:
    """Log in with username/password and mint a long-lived token.

    Returns:
        (long_lived_token, display_username)

    Raises:
        MALoginError: with a plain-language message.
    """
    url = url.rstrip("/")
    own_session = session is None
    sess = session or aiohttp.ClientSession()
    try:
        try:
            user, session_token = await login(url, username, password, sess)
        except LoginFailed:
            raise MALoginError(
                "Music Assistant did not accept that username and password. If you only ever "
                "open Music Assistant from the Home Assistant sidebar you may not have a Music "
                "Assistant password yet: set one in Music Assistant (your user profile), or "
                "use a long-lived token instead."
            ) from None
        except CannotConnect as e:
            if "403" in str(e):
                raise MALoginError(
                    "Music Assistant is not set up yet. Open it once, finish the setup, then try "
                    "again."
                ) from None
            raise MALoginError(
                f"Can't reach Music Assistant at {url}. Check the address and that the Music "
                "Assistant add-on is running."
            ) from None

        client = MusicAssistantClient(url, sess, token=session_token)
        try:
            await asyncio.wait_for(client.connect(), 15)
            token = await client.auth.create_token(token_name)
            try:
                # The login also created a 30-day session token we no longer need.
                await client.auth.logout()
            except Exception as e:
                logger.debug(f"Could not end the temporary Music Assistant session: {e}")
        except InsufficientPermissions:
            raise MALoginError(
                "This Music Assistant user is a guest and can't create the long-lived login the "
                "bridge needs. Use a regular user."
            ) from None
        except (CannotConnect, asyncio.TimeoutError, OSError, MusicAssistantClientException):
            raise MALoginError(
                f"Can't reach Music Assistant at {url}. Check the address and that the Music "
                "Assistant add-on is running."
            ) from None
        except MusicAssistantError as e:
            raise MALoginError(f"Music Assistant could not create a login token ({e}).") from None
        finally:
            try:
                await client.disconnect()
            except Exception:
                pass
        name = str(getattr(user, "display_name", "") or getattr(user, "username", "") or username)
        logger.info(f"Created a long-lived Music Assistant token for user {name}")
        return str(token), name
    finally:
        if own_session:
            await sess.close()


async def validate_token(
    url: str, token: str, *, session: Optional[aiohttp.ClientSession] = None
) -> str:
    """Check a pasted long-lived token by connecting once.

    Returns:
        The MA server version.

    Raises:
        MALoginError: with a plain-language message.
    """
    url = url.rstrip("/")
    own_session = session is None
    sess = session or aiohttp.ClientSession()
    client = MusicAssistantClient(url, sess, token=token)
    try:
        await asyncio.wait_for(client.connect(), 15)
        server_info = getattr(client, "server_info", None)
        return str(getattr(server_info, "server_version", "") or "")
    except (AuthenticationFailed, AuthenticationRequired, InvalidToken):
        raise MALoginError("Music Assistant did not accept that token.") from None
    except InvalidServerVersion as e:
        raise MALoginError(f"This Music Assistant version is not supported ({e}).") from None
    except (CannotConnect, asyncio.TimeoutError, OSError, MusicAssistantClientException):
        raise MALoginError(
            f"Can't reach Music Assistant at {url}. Check the address and that the Music "
            "Assistant add-on is running."
        ) from None
    finally:
        try:
            await client.disconnect()
        except Exception:
            pass
        if own_session:
            await sess.close()
