"""
Expose Music Assistant players as Qobuz Connect devices.

The manager owns the shared Music Assistant connection and keeps one Speaker
(Qobuz Connect device) running per exposed MA player or group: all of them by
default, or the ones ticked in the web UI. Each device gets a UUID derived from
the MA player id and a discovery port remembered in the settings file, so the
Qobuz app sees the same devices after a restart, even before MA answers.

Imports without the music-assistant extra: the web UI then explains that MA
support is missing instead of failing.
"""

import asyncio
import logging
import socket
import time
import uuid
from typing import Any, Callable, Optional

from qobuz_proxy.config import _SPEAKER_UUID_NAMESPACE, SpeakerConfig, slugify_name

from . import ma_support_error
from .settings import (
    DEFAULT_MA_URL,
    MA_PORT_RANGE_START,
    KnownPlayer,
    MASettings,
    MASettingsStore,
)
from .supervisor import detect_music_assistant_url

logger = logging.getLogger(__name__)

NAME_SUFFIX = " (MA)"
RECONCILE_DELAY_SECONDS = 1.0
# MA re-registers players slowly after a restart; don't drop devices for that.
MISSING_PLAYER_GRACE_SECONDS = 1800.0
SPEAKER_RETRY_DELAYS_SECONDS: tuple[float, ...] = (10.0, 30.0, 60.0)
SPEAKER_RETRY_STEADY_DELAY_SECONDS = 300.0
CONNECT_WAIT_SECONDS = 5.0

SpeakerFactory = Callable[[SpeakerConfig, Any], Any]


def ma_device_uuid(player_id: str) -> str:
    """Stable Qobuz Connect device UUID for an MA player (independent of names/hostname)."""
    return str(uuid.uuid5(_SPEAKER_UUID_NAMESPACE, f"music_assistant:{player_id}"))


def normalize_url(url: str) -> str:
    """Add a scheme and drop trailing slashes from a user-entered address."""
    url = url.strip()
    if url and "://" not in url:
        url = f"http://{url}"
    return url.rstrip("/")


def _port_is_free(bind_address: str, port: int) -> bool:
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
            s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            s.bind((bind_address, port))
        return True
    except OSError:
        return False


class MusicAssistantManager:
    """Runs the MA connection and one Speaker per exposed MA player."""

    def __init__(
        self,
        *,
        store: MASettingsStore,
        speaker_factory: SpeakerFactory,
        reserved_ports: Callable[[], set[int]],
        yaml_speakers: Callable[[], list[SpeakerConfig]],
        bind_address: str = "0.0.0.0",
        connection_factory: Optional[Callable[[str, str], Any]] = None,
        url_detector: Optional[Callable[[], Any]] = None,
        port_checker: Optional[Callable[[str, int], bool]] = None,
    ) -> None:
        self._store = store
        self._speaker_factory = speaker_factory
        self._reserved_ports = reserved_ports
        self._yaml_speakers = yaml_speakers
        self._bind_address = bind_address
        self._connection_factory = connection_factory
        self._url_detector = url_detector or detect_music_assistant_url
        self._port_checker = port_checker or _port_is_free

        self._settings = MASettings()
        self._support_error: Optional[str] = None
        self._connection: Optional[Any] = None
        self._qobuz_ready = False
        self._suggested_url = ""
        self._detecting = False

        self._speakers: dict[str, Any] = {}
        self._speaker_configs: dict[str, SpeakerConfig] = {}
        self._retry_tasks: dict[str, asyncio.Task[None]] = {}
        self._missing_since: dict[str, float] = {}
        self._lock = asyncio.Lock()
        self._reconcile_handle: Optional[asyncio.TimerHandle] = None
        self._tasks: set[asyncio.Task[Any]] = set()
        self._unsubscribers: list[Callable[[], None]] = []
        self._started = False
        self._last_exposed_names: list[str] = []

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    @property
    def connection(self) -> Optional[Any]:
        return self._connection

    @property
    def settings(self) -> MASettings:
        return self._settings

    async def start(self) -> None:
        """Load the settings and start connecting to MA (if set up)."""
        if self._started:
            return
        self._started = True
        self._settings = self._store.load()
        self._support_error = ma_support_error()
        if self._support_error:
            logger.warning(self._support_error)
            return
        if self._connection_factory is not None:
            self._connection = self._connection_factory(self._settings.url, self._settings.token)
        else:
            from .connection import MusicAssistantConnection

            self._connection = MusicAssistantConnection(self._settings.url, self._settings.token)
        self._unsubscribers = [
            self._connection.add_state_listener(lambda _status: self._schedule_reconcile()),
            self._connection.add_event_listener(None, self._on_ma_event),
        ]
        if self._settings.configured:
            logger.info(f"Connecting to Music Assistant at {self._settings.url}")
        else:
            logger.info("Music Assistant is not connected yet; set it up in the web UI")
        await self._connection.start()

    async def stop(self) -> None:
        """Stop all MA devices and the MA connection."""
        if self._reconcile_handle is not None:
            self._reconcile_handle.cancel()
            self._reconcile_handle = None
        for unsubscribe in self._unsubscribers:
            unsubscribe()
        self._unsubscribers = []
        await self._stop_all_speakers()
        for task in list(self._tasks):
            task.cancel()
        if self._connection is not None:
            await self._connection.stop()
        self._started = False

    async def set_qobuz_ready(self, ready: bool, *, restart: bool = False) -> None:
        """Start the MA devices once Qobuz is logged in; stop them on logout.

        ``restart`` re-creates running devices, e.g. after a new Qobuz login
        replaced the API client they use.
        """
        self._qobuz_ready = ready
        if not ready or restart:
            await self._stop_all_speakers()
        if ready:
            await self.reconcile()

    # ------------------------------------------------------------------
    # Web UI actions
    # ------------------------------------------------------------------

    async def connect(
        self, url: str, username: str = "", password: str = "", token: str = ""
    ) -> dict[str, Any]:
        """Log in to MA (or check a pasted token), save it and reconnect."""
        if self._support_error or self._connection is None:
            return {"ok": False, "error": self._support_error or "Music Assistant is unavailable"}
        url = normalize_url(url) or self._suggested_url or DEFAULT_MA_URL
        from .connection import MALoginError, login_and_create_token, validate_token

        try:
            if token.strip():
                await validate_token(url, token.strip())
                new_token, display_name = token.strip(), username.strip()
            else:
                if not username.strip() or not password:
                    return {
                        "ok": False,
                        "error": "Enter your Music Assistant username and password.",
                    }
                new_token, display_name = await login_and_create_token(
                    url, username.strip(), password
                )
        except MALoginError as e:
            logger.warning(f"Music Assistant login failed: {e}")
            return {"ok": False, "error": str(e)}

        self._settings.url = url
        self._settings.token = new_token
        self._settings.username = display_name
        self._save()
        logger.info(f"Music Assistant login saved for {url}")
        await self._connection.reconfigure(url, new_token)
        await self._wait_connected(CONNECT_WAIT_SECONDS)
        await self.reconcile()
        return {"ok": True}

    async def disconnect(self) -> None:
        """Forget the MA token and stop the MA devices (selection is kept)."""
        logger.info("Disconnecting from Music Assistant (forgetting the saved login)")
        self._settings.token = ""
        self._save()
        await self._stop_all_speakers()
        if self._connection is not None:
            await self._connection.reconfigure(self._settings.url, "")

    async def update_exposure(
        self,
        *,
        expose_all: Optional[bool] = None,
        selected: Optional[list[str]] = None,
        name_suffix: Optional[bool] = None,
    ) -> None:
        """Change which MA players are shown in the Qobuz app."""
        if expose_all is not None:
            self._settings.expose_all = expose_all
        if selected is not None:
            self._settings.selected_player_ids = list(dict.fromkeys(selected))
        if name_suffix is not None:
            self._settings.name_suffix = name_suffix
        self._save()
        logger.info(
            "Music Assistant players to share: "
            + (
                "all"
                if self._settings.expose_all
                else f"{len(self._settings.selected_player_ids)} selected"
            )
        )
        await self.reconcile()

    def status(self) -> dict[str, Any]:
        """State for the web UI (never includes the token)."""
        if not self._settings.url and not self._suggested_url and not self._detecting:
            # Look for MA once the panel is opened, to pre-fill its address
            self._detecting = True
            self._spawn(self._detect_url())
        conn_status = self._connection.status if self._connection is not None else None
        candidates = self._candidates()
        exposed = self._desired_configs(candidates) if self._settings.configured else {}
        yaml_ids = self._yaml_player_ids()
        players = []
        for pid, info in sorted(candidates.items(), key=lambda kv: kv[1]["name"].lower()):
            device: Optional[dict[str, Any]] = None
            speaker = self._speakers.get(pid)
            if speaker is not None:
                st = speaker.get_status()
                device = {"status": st.get("status"), "now_playing": st.get("now_playing")}
            elif pid in self._retry_tasks or (pid in exposed and self._qobuz_ready):
                device = {"status": "starting", "now_playing": None}
            players.append(
                {
                    "player_id": pid,
                    "ma_name": info["name"],
                    "qobuz_name": exposed[pid].name if pid in exposed else "",
                    "type": info["type"],
                    "available": info["available"],
                    "hidden": info["hidden"],
                    "selected": pid in self._settings.selected_player_ids,
                    "exposed": pid in exposed,
                    "in_yaml": pid in yaml_ids,
                    "device": device,
                }
            )
        return {
            "supported": self._support_error is None,
            "support_error": self._support_error or "",
            "configured": self._settings.configured,
            "connection": {
                "state": conn_status.state.value if conn_status else "not_configured",
                "message": conn_status.message if conn_status else (self._support_error or ""),
                "url": self._settings.url,
                "username": self._settings.username,
                "server_version": conn_status.server_version if conn_status else "",
                "qobuz_provider": conn_status.qobuz_provider if conn_status else None,
            },
            "suggested_url": self._settings.url or self._suggested_url or DEFAULT_MA_URL,
            "expose_all": self._settings.expose_all,
            "name_suffix": self._settings.name_suffix,
            "qobuz_ready": self._qobuz_ready,
            "players": players,
            "exposed_count": len(self._speakers),
        }

    # ------------------------------------------------------------------
    # Reconcile
    # ------------------------------------------------------------------

    def _schedule_reconcile(self) -> None:
        """Debounced reconcile (MA sends bursts of events)."""
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            return
        if self._reconcile_handle is not None:
            self._reconcile_handle.cancel()
        self._reconcile_handle = loop.call_later(
            RECONCILE_DELAY_SECONDS, lambda: self._spawn(self.reconcile())
        )

    def _on_ma_event(self, event: Any) -> None:
        name = getattr(getattr(event, "event", None), "value", "")
        if name in ("player_added", "player_removed", "player_updated", "player_config_updated"):
            self._schedule_reconcile()

    async def reconcile(self) -> None:
        """Start/stop/restart devices so they match the MA players to expose."""
        async with self._lock:
            candidates = self._candidates()
            desired: dict[str, SpeakerConfig] = {}
            if self._settings.configured and self._connection is not None:
                desired = self._desired_configs(candidates)
            if not self._qobuz_ready:
                desired = {}

            for pid in list(self._speakers):
                running = self._speaker_configs.get(pid)
                wanted = desired.get(pid)
                if (
                    wanted is None
                    or running is None
                    or ((wanted.name, wanted.http_port) != (running.name, running.http_port))
                ):
                    await self._stop_speaker(pid)
            for pid in list(self._retry_tasks):
                if pid not in desired:
                    self._retry_tasks.pop(pid).cancel()

            to_start = [
                sc
                for pid, sc in desired.items()
                if pid not in self._speakers and pid not in self._retry_tasks
            ]
            if to_start:
                await asyncio.gather(*(self._start_speaker(sc) for sc in to_start))

            names = sorted(sc.name for sc in desired.values())
            if names != self._last_exposed_names:
                self._last_exposed_names = names
                if names:
                    logger.info(
                        f"Music Assistant players in the Qobuz app ({len(names)}): "
                        + ", ".join(names)
                    )
                elif self._qobuz_ready and self._settings.configured:
                    logger.info("No Music Assistant players are shared with the Qobuz app")

    def _candidates(self) -> dict[str, dict[str, Any]]:
        """MA players we could expose: live list when connected, else the known ones."""
        known = self._settings.known_players
        conn = self._connection
        now = time.monotonic()
        changed = False
        result: dict[str, dict[str, Any]] = {}
        if conn is not None and conn.is_connected:
            live = {p.player_id: p for p in conn.list_players() if p.enabled}
            for pid, p in live.items():
                self._missing_since.pop(pid, None)
                kp = known.get(pid)
                if kp is None:
                    port = self._assign_port(pid)
                    known[pid] = KnownPlayer(pid, p.name, p.player_type, port, p.hidden)
                    changed = True
                elif (kp.name, kp.player_type, kp.hidden) != (p.name, p.player_type, p.hidden):
                    kp.name, kp.player_type, kp.hidden = p.name, p.player_type, p.hidden
                    changed = True
                result[pid] = {
                    "name": p.name,
                    "type": p.player_type,
                    "available": p.available,
                    "hidden": p.hidden,
                }
            for pid in list(known):
                if pid in live:
                    continue
                since = self._missing_since.setdefault(pid, now)
                if now - since > MISSING_PLAYER_GRACE_SECONDS:
                    logger.info(f"Music Assistant player {known[pid].name} is gone; forgetting it")
                    del known[pid]
                    self._missing_since.pop(pid, None)
                    changed = True
                    continue
                kp = known[pid]
                result[pid] = {
                    "name": kp.name,
                    "type": kp.player_type,
                    "available": False,
                    "hidden": kp.hidden,
                }
        else:
            for pid, kp in known.items():
                result[pid] = {
                    "name": kp.name,
                    "type": kp.player_type,
                    "available": False,
                    "hidden": kp.hidden,
                }
        if changed:
            self._save()
        return result

    def _desired_configs(self, candidates: dict[str, dict[str, Any]]) -> dict[str, SpeakerConfig]:
        """SpeakerConfigs for the players to expose, with stable deduplicated names."""
        settings = self._settings
        if settings.expose_all:
            exposed = [pid for pid, info in candidates.items() if not info["hidden"]]
        else:
            exposed = [pid for pid in settings.selected_player_ids if pid in candidates]
        yaml = self._yaml_speakers()
        yaml_ids = self._yaml_player_ids()
        used = {slugify_name(s.name) for s in yaml}
        configs: dict[str, SpeakerConfig] = {}
        for pid in sorted(exposed):
            if pid in yaml_ids:
                continue
            kp = settings.known_players.get(pid)
            if kp is None:
                continue
            base = candidates[pid]["name"] + (NAME_SUFFIX if settings.name_suffix else "")
            name, n = base, 2
            while slugify_name(name) in used:
                name = f"{base} {n}"
                n += 1
            used.add(slugify_name(name))
            configs[pid] = SpeakerConfig(
                name=name,
                uuid=ma_device_uuid(pid),
                backend_type="music_assistant",
                max_quality=27,
                http_port=kp.http_port,
                bind_address=self._bind_address,
                ma_player_id=pid,
            )
        return configs

    def _assign_port(self, player_id: str) -> int:
        taken = set(self._reserved_ports()) | {
            kp.http_port for pid, kp in self._settings.known_players.items() if pid != player_id
        }
        port = MA_PORT_RANGE_START
        while port in taken or not self._port_checker(self._bind_address, port):
            port += 1
        return port

    def _yaml_player_ids(self) -> set[str]:
        return {
            s.ma_player_id
            for s in self._yaml_speakers()
            if s.backend_type == "music_assistant" and s.ma_player_id
        }

    # ------------------------------------------------------------------
    # Speakers
    # ------------------------------------------------------------------

    async def _start_speaker(self, sc: SpeakerConfig) -> bool:
        pid = sc.ma_player_id
        speaker = self._speaker_factory(sc, self._connection)
        try:
            ok = bool(await speaker.start())
        except Exception as e:
            logger.error(f"[{sc.name}] Failed to start: {e}", exc_info=True)
            ok = False
        if ok:
            self._speakers[pid] = speaker
            self._speaker_configs[pid] = sc
            return True
        self._retry_tasks[pid] = asyncio.create_task(self._retry_speaker(sc))
        return False

    async def _retry_speaker(self, sc: SpeakerConfig) -> None:
        pid = sc.ma_player_id
        attempt = 0
        try:
            while True:
                delays = SPEAKER_RETRY_DELAYS_SECONDS
                delay = (
                    delays[attempt] if attempt < len(delays) else SPEAKER_RETRY_STEADY_DELAY_SECONDS
                )
                logger.info(f"[{sc.name}] Retrying in {delay:.0f}s")
                await asyncio.sleep(delay)
                attempt += 1
                async with self._lock:
                    if not self._qobuz_ready or pid in self._speakers:
                        return
                    speaker = self._speaker_factory(sc, self._connection)
                    try:
                        ok = bool(await speaker.start())
                    except Exception as e:
                        logger.error(f"[{sc.name}] Failed to start: {e}")
                        ok = False
                    if ok:
                        self._speakers[pid] = speaker
                        self._speaker_configs[pid] = sc
                        return
        except asyncio.CancelledError:
            pass
        finally:
            if self._retry_tasks.get(pid) is asyncio.current_task():
                self._retry_tasks.pop(pid, None)

    async def _stop_speaker(self, pid: str) -> None:
        speaker = self._speakers.pop(pid, None)
        self._speaker_configs.pop(pid, None)
        if speaker is not None:
            try:
                await speaker.stop()
            except Exception as e:
                logger.warning(f"Error stopping Music Assistant device {pid}: {e}")

    async def _stop_all_speakers(self) -> None:
        for task in list(self._retry_tasks.values()):
            task.cancel()
        self._retry_tasks.clear()
        for pid in list(self._speakers):
            await self._stop_speaker(pid)
        self._last_exposed_names = []

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    def _save(self) -> None:
        try:
            self._store.save(self._settings)
        except OSError:
            pass  # logged by the store; keep running with the in-memory settings

    async def _wait_connected(self, timeout: float) -> None:
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            conn = self._connection
            if conn is None or conn.is_connected:
                return
            if conn.status.state.value in ("auth_failed", "not_configured"):
                return
            await asyncio.sleep(0.1)

    async def _detect_url(self) -> None:
        try:
            self._suggested_url = await self._url_detector()
        except Exception as e:
            logger.debug(f"Music Assistant address detection failed: {e}")

    def _spawn(self, coro: Any) -> None:
        task = asyncio.ensure_future(coro)
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)
