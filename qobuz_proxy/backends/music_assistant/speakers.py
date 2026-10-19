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
import itertools
import logging
import socket
import time
import uuid
from pathlib import Path
from typing import Any, Callable, Optional

from qobuz_proxy.backends.base import AudioBackend
from qobuz_proxy.config import _SPEAKER_UUID_NAMESPACE, SpeakerConfig, slugify_name

from . import LoginError, settings
from .settings import DEFAULT_URL, KnownPlayer

logger = logging.getLogger(__name__)

NAME_SUFFIX = " (MA)"
FIRST_PORT = 8700  # clear of the main web port (8689) and config.yaml speakers (8690+)
UPDATE_DELAY_SECONDS = 1.0  # MA sends player events in bursts
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
        self._connection: Optional[Any] = None
        self._is_qobuz_logged_in = False
        self._speakers: dict[str, Any] = {}
        self._running_configs: dict[str, SpeakerConfig] = {}
        self._retry_tasks: dict[str, asyncio.Task[None]] = {}
        self._missing_since: dict[str, float] = {}
        self._lock = asyncio.Lock()
        self._update_timer: Optional[asyncio.TimerHandle] = None
        self._update_task: Optional[asyncio.Task[None]] = None
        self._logged_speaker_names: list[str] = []

    @property
    def connection(self) -> Optional[Any]:
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
        self._connection.add_event_listener(None, self._on_ma_event)
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

    async def set_qobuz_logged_in(self, ready: bool, restart: bool = False) -> None:
        """Start the speakers once Qobuz is logged in, stop them on logout.

        ``restart`` builds them again, e.g. after a new Qobuz login replaced
        the API client they use.
        """
        self._is_qobuz_logged_in = ready
        if not ready or restart:
            await self._stop_all_speakers()
        if ready:
            await self.update_speakers()

    async def connect(self, url: str, username: str, password: str) -> None:
        """Log in to MA, save the new token and reconnect.

        Raises:
            LoginError: with a message for the web UI.
        """
        if self._connection is None:
            raise LoginError(self._unsupported_reason)
        from .connection import login_and_create_token

        if not username.strip() or not password:
            raise LoginError("Enter your Music Assistant username and password.")
        url = normalize_url(url) or DEFAULT_URL
        try:
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

    def status(self) -> dict[str, Any]:
        """What the web UI shows; never the token."""
        conn = self._connection.status if self._connection is not None else None
        live = self._update_known_players()
        wanted = self._wanted_speakers() if self._settings.is_configured else {}
        known = self._settings.known_players
        return {
            "is_configured": self._settings.is_configured,
            "connection": {
                "state": conn.state.value if conn else "not_configured",
                "message": conn.message if conn else self._unsupported_reason,
                "server_version": conn.server_version if conn else "",
                "has_qobuz_provider": conn.has_qobuz_provider if conn else False,
                "url": self._settings.url or DEFAULT_URL,
                "username": self._settings.username,
            },
            "expose_all": self._settings.expose_all,
            "players": [
                {
                    "player_id": pid,
                    "name": p.name,
                    "type": p.player_type,
                    "available": live.get(pid, False),
                    "hidden": p.hidden,
                    "selected": pid in self._settings.selected_player_ids,
                    "speaker_name": wanted[pid].name if pid in wanted else "",
                }
                for pid, p in sorted(known.items(), key=lambda kv: kv[1].name.lower())
            ],
            "speaker_count": len(self._speakers),
        }

    async def update_speakers(self) -> None:
        """Start, stop or restart speakers so they match the MA players to expose."""
        async with self._lock:
            self._update_known_players()
            ready = self._is_qobuz_logged_in and self._settings.is_configured and self._connection
            wanted = self._wanted_speakers() if ready else {}

            for pid, running in list(self._running_configs.items()):
                want = wanted.get(pid)
                if want is not None and (want.name, want.http_port) == (
                    running.name,
                    running.http_port,
                ):
                    continue
                if want is not None and self._is_playing(pid):
                    continue  # a rename restarts the speaker: wait until it is idle
                await self._stop_speaker(pid)
            for pid in list(self._retry_tasks):
                if pid not in wanted:
                    self._retry_tasks.pop(pid).cancel()

            new = [
                pid for pid in wanted if pid not in self._speakers and pid not in self._retry_tasks
            ]
            started = await asyncio.gather(*(self._start_speaker(pid, wanted[pid]) for pid in new))
            for pid, ok in zip(new, started):
                if not ok:
                    self._retry_tasks[pid] = asyncio.create_task(
                        self._retry_start(pid, wanted[pid])
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
        """Reconcile once soon; later events before then ride along."""
        if self._update_timer is not None:
            return

        def run() -> None:
            self._update_timer = None
            self._update_task = asyncio.create_task(self.update_speakers())

        self._update_timer = asyncio.get_running_loop().call_later(UPDATE_DELAY_SECONDS, run)

    def _on_ma_event(self, event: Any) -> None:
        if event.event.value in PLAYER_EVENTS:
            self._update_speakers_soon()

    def _update_known_players(self) -> dict[str, bool]:
        """Bring the known players up to date with MA; returns live player ids -> available."""
        if self._connection is None or not self._connection.is_connected:
            return {}
        known = self._settings.known_players
        live = {p.player_id: p for p in self._connection.players()}
        changed = False
        for pid, p in live.items():
            self._missing_since.pop(pid, None)
            entry = KnownPlayer(p.name, p.type, 0, p.hidden)
            if pid not in known:
                entry.http_port = self._free_port()
                known[pid] = entry
                changed = True
            elif (known[pid].name, known[pid].player_type, known[pid].hidden) != (
                entry.name,
                entry.player_type,
                entry.hidden,
            ):
                entry.http_port = known[pid].http_port
                known[pid] = entry
                changed = True
        now = time.monotonic()
        for pid in [pid for pid in known if pid not in live]:
            if now - self._missing_since.setdefault(pid, now) > MISSING_PLAYER_GRACE_SECONDS:
                logger.info(f"Music Assistant player {known[pid].name} is gone; forgetting it")
                del known[pid]
                del self._missing_since[pid]
                self._settings.sessions.pop(pid, None)
                changed = True
        if changed:
            self._save()
        return {pid: p.available for pid, p in live.items()}

    def _wanted_speakers(self) -> dict[str, SpeakerConfig]:
        """Speaker configs for the players to expose, with unique names."""
        known = self._settings.known_players
        if self._settings.expose_all:
            exposed = [pid for pid, p in known.items() if not p.hidden]
        else:
            exposed = [pid for pid in self._settings.selected_player_ids if pid in known]
        used: set[str] = set()
        configs = {}
        for pid in sorted(exposed):
            base = known[pid].name + NAME_SUFFIX
            name, n = base, 2
            while slugify_name(name) in used:
                name, n = f"{base} {n}", n + 1
            used.add(slugify_name(name))
            configs[pid] = SpeakerConfig(
                name=name,
                uuid=speaker_uuid(pid),
                backend_type="music_assistant",
                max_quality=27,  # MA streams with its own quality; this only checks availability
                http_port=known[pid].http_port,
                bind_address=self._bind_address,
            )
        return configs

    def _free_port(self) -> int:
        taken = {p.http_port for p in self._settings.known_players.values()}
        port = FIRST_PORT
        while port in taken or not self._port_is_free(self._bind_address, port):
            port += 1
        return port

    async def _start_speaker(self, pid: str, config: SpeakerConfig) -> bool:
        from .backend import MusicAssistantBackend

        backend = MusicAssistantBackend(
            self._connection,
            pid,
            config.name,
            session=self._settings.sessions.get(pid),
            save_session=lambda session: self._save_session(pid, session),
        )
        speaker = self._make_speaker(config, backend)
        if not await speaker.start():
            return False
        self._speakers[pid] = speaker
        self._running_configs[pid] = config
        return True

    async def _retry_start(self, pid: str, config: SpeakerConfig) -> None:
        """Keep trying to start a speaker that failed to start (e.g. its port was taken)."""
        try:
            for attempt in itertools.count():
                delay = START_RETRY_DELAYS_SECONDS[
                    min(attempt, len(START_RETRY_DELAYS_SECONDS) - 1)
                ]
                logger.info(f"[{config.name}] Retrying in {delay:.0f}s")
                await asyncio.sleep(delay)
                async with self._lock:
                    if await self._start_speaker(pid, config):
                        return
        finally:
            if self._retry_tasks.get(pid) is asyncio.current_task():
                del self._retry_tasks[pid]

    def _is_playing(self, pid: str) -> bool:
        return self._speakers[pid].get_status()["status"] in ("playing", "paused")

    async def _stop_speaker(self, pid: str) -> None:
        self._running_configs.pop(pid)
        await self._speakers.pop(pid).stop()

    async def _stop_all_speakers(self) -> None:
        async with self._lock:
            for task in self._retry_tasks.values():
                task.cancel()
            self._retry_tasks.clear()
            for pid in list(self._speakers):
                await self._stop_speaker(pid)
            self._logged_speaker_names = []

    def _save_session(self, pid: str, session: Optional[dict[str, Any]]) -> None:
        sessions = self._settings.sessions
        if sessions.get(pid) == session:
            return
        if session is None:
            del sessions[pid]
        else:
            sessions[pid] = session
        self._save()

    def _save(self) -> None:
        settings.save(self._settings_path, self._settings)

    async def _wait_until_connected(self) -> None:
        """Give a new login a few seconds to connect, so the web UI shows the result."""
        deadline = time.monotonic() + CONNECT_WAIT_SECONDS
        while time.monotonic() < deadline and self._connection is not None:
            if self._connection.status.state.value not in ("connecting", "disconnected"):
                return
            await asyncio.sleep(0.1)
