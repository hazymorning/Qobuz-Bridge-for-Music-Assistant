"""
Qobuz Connect speakers for Music Assistant players.

Runs the shared MA connection and one Speaker per exposed MA player or group.
Each speaker's UUID comes from the MA player id and its discovery port is kept
in the settings, so the Qobuz app sees the same speakers after a restart, even
before MA answers.

Imports without the music-assistant extra (Python < 3.13); MA support then
stays off and the web UI says why.
"""

import asyncio
import contextlib
import itertools
import logging
import socket
import time
import uuid
from pathlib import Path
from typing import TYPE_CHECKING, Any, Callable, Optional

from qobuz_proxy.backends.base import AudioBackend
from qobuz_proxy.config import _SPEAKER_UUID_NAMESPACE, SpeakerConfig, slugify_name

from . import LoginError, settings
from .settings import DEFAULT_URL, KnownPlayer

if TYPE_CHECKING:
    from .connection import MusicAssistantConnection

logger = logging.getLogger(__name__)

NAME_SUFFIX = " (MA)"
NAME_MAX_LENGTH = 40  # characters in a name the user gives a speaker
FIRST_PORT = 8700  # clear of the main web port (8689) and config.yaml speakers (8690+)
UPDATE_DELAY_SECONDS = 1.0
# MA re-registers players slowly after a restart; don't drop their speakers for that.
MISSING_PLAYER_GRACE_SECONDS = 1800.0
START_RETRY_DELAYS_SECONDS = (10.0, 30.0, 60.0, 300.0)
CONNECT_WAIT_SECONDS = 5.0
PLAYER_EVENTS = {"player_added", "player_removed", "player_updated", "player_config_updated"}

SpeakerFactory = Callable[[SpeakerConfig, AudioBackend], Any]


def speaker_uuid(player_id: str) -> str:
    """Speaker UUID for an MA player; independent of names and hostname."""
    return str(uuid.uuid5(_SPEAKER_UUID_NAMESPACE, f"music_assistant:{player_id}"))


def normalize_url(url: str) -> str:
    """Add a scheme and drop trailing slashes from a typed-in address."""
    url = url.strip().rstrip("/")
    if url and "://" not in url:
        url = f"http://{url}"
    return url


def port_is_free(bind_address: str, port: int) -> bool:
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
            s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            s.bind((bind_address, port))
        return True
    except OSError:
        return False


class MusicAssistantSpeakers:
    """Runs the MA connection and one speaker per exposed MA player."""

    def __init__(
        self,
        settings_path: Path,
        make_speaker: SpeakerFactory,
        bind_address: str = "0.0.0.0",
        client_factory: Optional[Callable[..., Any]] = None,
        port_is_free: Callable[[str, int], bool] = port_is_free,
    ) -> None:
        self._settings_path = settings_path
        self._make_speaker = make_speaker
        self._bind_address = bind_address
        self._client_factory = client_factory
        self._port_is_free = port_is_free

        self._settings = settings.Settings()
        self._unsupported_reason = ""  # why MA support is off, if it is
        self._connection: Optional["MusicAssistantConnection"] = None
        self._is_qobuz_logged_in = False
        self._speakers: dict[str, Any] = {}
        self._running_configs: dict[str, SpeakerConfig] = {}
        self._retry_tasks: dict[str, asyncio.Task[None]] = {}
        self._missing_since: dict[str, float] = {}
        self._lock = asyncio.Lock()
        self._login_lock = asyncio.Lock()
        self._update_timer: Optional[asyncio.TimerHandle] = None
        self._update_task: Optional[asyncio.Task[None]] = None
        self._logged_speaker_names: list[str] = []

    @property
    def connection(self) -> Optional["MusicAssistantConnection"]:
        return self._connection

    async def start(self) -> None:
        """Load the settings and start connecting to MA."""
        self._settings = settings.load(self._settings_path)
        try:
            from .connection import MusicAssistantConnection
        except ImportError as e:
            self._unsupported_reason = (
                f"Music Assistant support is not installed (needs Python 3.13+): {e}"
            )
            logger.warning(self._unsupported_reason)
            return
        extra = {"client_factory": self._client_factory} if self._client_factory else {}
        self._connection = MusicAssistantConnection(
            self._settings.url, self._settings.token, **extra
        )
        self._connection.add_status_listener(lambda _status: self._update_speakers_soon())
        self._connection.add_event_listener(None, self._on_event)
        if self._settings.is_configured:
            logger.info(f"Connecting to Music Assistant at {self._settings.url}")
        else:
            logger.info("Music Assistant is not connected yet; set it up in the web UI")
        await self._connection.start()

    async def stop(self) -> None:
        """Stop every speaker and the MA connection; MA itself keeps playing."""
        if self._update_timer is not None:
            self._update_timer.cancel()
        if self._update_task is not None:
            self._update_task.cancel()
        await self._stop_all_speakers()
        if self._connection is not None:
            await self._connection.stop()

    async def set_qobuz_logged_in(self, logged_in: bool, restart: bool = False) -> None:
        """Start the speakers once Qobuz is logged in, stop them on logout.

        ``restart`` builds them again, e.g. after a new Qobuz login replaced
        the API client they use.
        """
        self._is_qobuz_logged_in = logged_in
        if not logged_in or restart:
            await self._stop_all_speakers()
        if logged_in:
            await self.update_speakers()

    async def connect(
        self, url: str = "", username: str = "", password: str = "", token: str = ""
    ) -> None:
        """Log in to MA (or check a token made in MA), save the token and reconnect.

        Raises:
            LoginError: with a message for the web UI.
        """
        if self._connection is None:
            raise LoginError(self._unsupported_reason)
        from .connection import check_token, login_and_create_token

        token = token.strip()
        if not token and (not username.strip() or not password):
            raise LoginError("Enter your Music Assistant username and password, or a token.")
        url = normalize_url(url) or DEFAULT_URL
        async with self._login_lock:
            try:
                if token:
                    name = await check_token(url, token)
                else:
                    token, name = await login_and_create_token(url, username.strip(), password)
            except LoginError as e:
                logger.warning(f"Music Assistant login failed: {e}")
                raise
            self._settings.url, self._settings.token, self._settings.username = url, token, name
            self._save()
            logger.info(f"Music Assistant login saved for {url}")
            await self._connection.reconnect(url, token)
        await self._wait_until_connected()
        await self.update_speakers()

    async def disconnect(self) -> None:
        """Forget the MA token and stop the speakers; the player choice is kept."""
        logger.info("Disconnecting from Music Assistant (forgetting the saved login)")
        async with self._login_lock:
            self._settings.token = ""
            self._save()
            await self._stop_all_speakers()
            if self._connection is not None:
                await self._connection.reconnect(self._settings.url, "")

    async def set_exposed_players(
        self, expose_all: Optional[bool] = None, selected: Optional[list[str]] = None
    ) -> None:
        """Change which MA players appear in the Qobuz app."""
        if expose_all is not None:
            self._settings.expose_all = expose_all
        if selected is not None:
            self._settings.selected_player_ids = list(dict.fromkeys(selected))
        self._save()
        logger.info(
            "Music Assistant players in the Qobuz app: "
            + (
                "all"
                if self._settings.expose_all
                else f"{len(self._settings.selected_player_ids)} selected"
            )
        )
        await self.update_speakers()

    async def set_speaker_name(self, player_id: str, name: str) -> None:
        """Give a player's speaker the user's own name; an empty name brings back the default.

        Raises:
            ValueError: with a message for the web UI.
        """
        name = " ".join(name.split())
        if player_id not in self._settings.known_players:
            raise ValueError("That Music Assistant player is not known (any more).")
        if len(name) > NAME_MAX_LENGTH:
            raise ValueError(f"Keep the name to {NAME_MAX_LENGTH} characters.")
        if not name.isprintable():
            raise ValueError("The name can only have letters, numbers, spaces and punctuation.")
        others = [c.name for i, c in self._wanted_speakers().items() if i != player_id]
        others += [n for i, n in self._settings.speaker_names.items() if i != player_id]
        if name and slugify_name(name) in {slugify_name(other) for other in others}:
            raise ValueError(f"Another speaker is already called “{name}”.")
        if name:
            self._settings.speaker_names[player_id] = name
        else:
            self._settings.speaker_names.pop(player_id, None)
        self._save()
        logger.info(
            f"Speaker name for {self._settings.known_players[player_id].name}: "
            + (name or "the default")
        )
        await self.update_speakers()

    def status(self) -> dict[str, Any]:
        """What the web UI shows; never the token."""
        live = self._update_known_players()
        wanted = self._wanted_speakers() if self._settings.is_configured else {}
        known = self._settings.known_players
        return {
            "is_configured": self._settings.is_configured,
            "connection": {
                **self._connection_status(),
                "url": self._settings.url or DEFAULT_URL,
                "username": self._settings.username,
            },
            "expose_all": self._settings.expose_all,
            "players": [
                {
                    "player_id": player_id,
                    "name": p.name,
                    "type": p.player_type,
                    "available": live.get(player_id, False),
                    "hidden": p.hidden,
                    "selected": player_id in self._settings.selected_player_ids,
                    "speaker_name": wanted[player_id].name if player_id in wanted else "",
                    "custom_name": self._settings.speaker_names.get(player_id, ""),
                    "default_name": p.name + NAME_SUFFIX,
                }
                for player_id, p in sorted(known.items(), key=lambda kv: kv[1].name.lower())
            ],
            "speaker_count": len(self._speakers),
        }

    def _connection_status(self) -> dict[str, Any]:
        if self._connection is None:
            return {
                "state": "not_configured",
                "message": self._unsupported_reason,
                "server_version": "",
                "has_qobuz_provider": False,
            }
        status = self._connection.status
        return {
            "state": status.state.value,
            "message": status.message,
            "server_version": status.server_version,
            "has_qobuz_provider": status.has_qobuz_provider,
        }

    async def update_speakers(self) -> None:
        """Start, stop or restart speakers so they match the MA players to expose."""
        async with self._lock:
            self._update_known_players()
            ready = self._is_qobuz_logged_in and self._settings.is_configured and self._connection
            wanted = self._wanted_speakers() if ready else {}

            for player_id, running in list(self._running_configs.items()):
                want = wanted.get(player_id)
                if want is not None and (want.name, want.http_port) == (
                    running.name,
                    running.http_port,
                ):
                    continue
                if want is not None and self._is_playing(player_id):
                    continue  # a rename restarts the speaker: wait until it is idle
                await self._stop_speaker(player_id)
            for player_id in list(self._retry_tasks):
                if player_id not in wanted:
                    self._retry_tasks.pop(player_id).cancel()

            new = [
                player_id
                for player_id in wanted
                if player_id not in self._speakers and player_id not in self._retry_tasks
            ]
            started = await asyncio.gather(
                *(self._start_speaker(player_id, wanted[player_id]) for player_id in new)
            )
            for player_id, ok in zip(new, started):
                if not ok:
                    self._retry_tasks[player_id] = asyncio.create_task(
                        self._retry_start(player_id, wanted[player_id])
                    )

            names = sorted(c.name for c in wanted.values())
            if names != self._logged_speaker_names:
                self._logged_speaker_names = names
                logger.info(
                    f"Music Assistant players in the Qobuz app ({len(names)}): {', '.join(names)}"
                    if names
                    else "No Music Assistant players are in the Qobuz app"
                )

    def _update_speakers_soon(self) -> None:
        """Update the speakers once, soon; MA sends player events in bursts."""
        if self._update_timer is not None:
            return

        def run() -> None:
            self._update_timer = None
            self._update_task = asyncio.create_task(self.update_speakers())

        self._update_timer = asyncio.get_running_loop().call_later(UPDATE_DELAY_SECONDS, run)

    def _on_event(self, event: Any) -> None:
        if event.event.value in PLAYER_EVENTS:
            self._update_speakers_soon()

    def _update_known_players(self) -> dict[str, bool]:
        """Bring the known players up to date with MA; returns live player ids -> available."""
        if self._connection is None or not self._connection.is_connected:
            return {}
        known = self._settings.known_players
        live = {p.player_id: p for p in self._connection.players()}
        changed = False
        for player_id, p in live.items():
            self._missing_since.pop(player_id, None)
            entry = KnownPlayer(p.name, p.type, 0, p.hidden)
            if player_id not in known:
                entry.http_port = self._free_port()
                known[player_id] = entry
                changed = True
            elif (known[player_id].name, known[player_id].player_type, known[player_id].hidden) != (
                entry.name,
                entry.player_type,
                entry.hidden,
            ):
                entry.http_port = known[player_id].http_port
                known[player_id] = entry
                changed = True
        now = time.monotonic()
        for player_id in [player_id for player_id in known if player_id not in live]:
            if now - self._missing_since.setdefault(player_id, now) > MISSING_PLAYER_GRACE_SECONDS:
                logger.info(
                    f"Music Assistant player {known[player_id].name} is gone; forgetting it"
                )
                del known[player_id]
                del self._missing_since[player_id]
                self._settings.sessions.pop(player_id, None)
                self._settings.speaker_names.pop(player_id, None)
                changed = True
        if changed:
            self._save()
        return {player_id: p.available for player_id, p in live.items()}

    def _wanted_speakers(self) -> dict[str, SpeakerConfig]:
        """Speaker configs for the players to expose, with unique names."""
        known = self._settings.known_players
        if self._settings.expose_all:
            exposed = [player_id for player_id, p in known.items() if not p.hidden]
        else:
            exposed = [
                player_id for player_id in self._settings.selected_player_ids if player_id in known
            ]
        used: set[str] = set()
        configs = {}
        for player_id in sorted(exposed):
            base = (
                self._settings.speaker_names.get(player_id) or known[player_id].name + NAME_SUFFIX
            )
            name, n = base, 2
            while slugify_name(name) in used:
                name, n = f"{base} {n}", n + 1
            used.add(slugify_name(name))
            configs[player_id] = SpeakerConfig(
                name=name,
                uuid=speaker_uuid(player_id),
                backend_type="music_assistant",
                max_quality=27,  # MA streams with its own quality; this only checks availability
                http_port=known[player_id].http_port,
                bind_address=self._bind_address,
            )
        return configs

    def _free_port(self) -> int:
        taken = {p.http_port for p in self._settings.known_players.values()}
        port = FIRST_PORT
        while port in taken or not self._port_is_free(self._bind_address, port):
            port += 1
        return port

    async def _start_speaker(self, player_id: str, config: SpeakerConfig) -> bool:
        from .backend import MusicAssistantBackend

        assert self._connection is not None  # no speaker is wanted before start() made it
        backend = MusicAssistantBackend(
            self._connection,
            player_id,
            config.name,
            session=self._settings.sessions.get(player_id),
            save_session=lambda session: self._save_session(player_id, session),
        )
        speaker = self._make_speaker(config, backend)
        if not await speaker.start():
            return False
        self._speakers[player_id] = speaker
        self._running_configs[player_id] = config
        return True

    async def _retry_start(self, player_id: str, config: SpeakerConfig) -> None:
        """Keep trying to start a speaker that failed to start (e.g. its port was taken)."""
        try:
            for attempt in itertools.count():
                delay = START_RETRY_DELAYS_SECONDS[
                    min(attempt, len(START_RETRY_DELAYS_SECONDS) - 1)
                ]
                logger.info(f"[{config.name}] Retrying in {delay:.0f}s")
                await asyncio.sleep(delay)
                async with self._lock:
                    if await self._start_speaker(player_id, config):
                        return
        finally:
            if self._retry_tasks.get(player_id) is asyncio.current_task():
                del self._retry_tasks[player_id]

    def _is_playing(self, player_id: str) -> bool:
        return self._speakers[player_id].get_status()["status"] in ("playing", "paused")

    async def _stop_speaker(self, player_id: str) -> None:
        self._running_configs.pop(player_id)
        await self._speakers.pop(player_id).stop()

    async def _stop_all_speakers(self) -> None:
        async with self._lock:
            for task in self._retry_tasks.values():
                task.cancel()
            self._retry_tasks.clear()
            for player_id in list(self._speakers):
                await self._stop_speaker(player_id)
            self._logged_speaker_names = []

    def _save_session(self, player_id: str, session: Optional[dict[str, Any]]) -> None:
        sessions = self._settings.sessions
        if sessions.get(player_id) == session:
            return
        if session is None:
            del sessions[player_id]
        else:
            sessions[player_id] = session
        self._save()

    def _save(self) -> None:
        settings.save(self._settings_path, self._settings)

    async def _wait_until_connected(self) -> None:
        """Give a new login a few seconds to connect, so the web UI shows the result."""
        assert self._connection is not None
        settled = asyncio.Event()

        def on_status(status: Any) -> None:
            if status.state.value not in ("connecting", "disconnected"):
                settled.set()

        on_status(self._connection.status)
        self._connection.add_status_listener(on_status)
        try:
            with contextlib.suppress(asyncio.TimeoutError):
                await asyncio.wait_for(settled.wait(), CONNECT_WAIT_SECONDS)
        finally:
            self._connection.remove_status_listener(on_status)
