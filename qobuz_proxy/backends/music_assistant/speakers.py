"""
Qobuz Connect speakers for Music Assistant players.

Runs the shared MA connection and one Speaker per exposed MA player or group,
plus one per party speaker. Each speaker's UUID comes from the MA player id (or
the party id) and its discovery port is kept in the settings, so the Qobuz app
sees the same speakers after a restart, even before MA answers.

Imports without the music-assistant extra (Python < 3.13); MA support then
stays off and the web UI says why.
"""

import asyncio
import contextlib
import itertools
import logging
import secrets
import socket
import time
import uuid
from dataclasses import asdict
from pathlib import Path
from typing import TYPE_CHECKING, Any, Callable, Optional

from qobuz_proxy.backends.base import AudioBackend
from qobuz_proxy.config import _SPEAKER_UUID_NAMESPACE, SpeakerConfig, slugify_name

from . import LoginError, settings
from .home_assistant import NOT_AVAILABLE, HomeAssistant
from .settings import (
    AFTER_PARTY,
    BUSY_SPEAKERS,
    DEFAULT_URL,
    GROUPINGS,
    ON_LEAVE,
    KnownPlayer,
    PartySpeaker,
)

if TYPE_CHECKING:
    from .connection import MusicAssistantConnection
    from .party import Party

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
PARTY = "party:"  # speaker key of a party speaker: PARTY + its party id
PARTY_FIELDS = {
    "name": str,
    "main_player_id": str,
    "grouping": str,
    "member_ids": list,
    "main_entity_id": str,
    "busy_speakers": str,
    "after_party": str,
    "on_leave": str,
}
ENTITY_ID_MAX_LENGTH = 255

SpeakerFactory = Callable[[SpeakerConfig, AudioBackend], Any]


def speaker_uuid(key: str) -> str:
    """Speaker UUID for an MA player id or a party key; independent of names and hostname."""
    return str(uuid.uuid5(_SPEAKER_UUID_NAMESPACE, f"music_assistant:{key}"))


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
    """Runs the MA connection and one speaker per exposed MA player and per party speaker.

    Speakers are keyed by the MA player id, or PARTY + the party id.
    """

    def __init__(
        self,
        settings_path: Path,
        make_speaker: SpeakerFactory,
        bind_address: str = "0.0.0.0",
        client_factory: Optional[Callable[..., Any]] = None,
        port_is_free: Callable[[str, int], bool] = port_is_free,
        home_assistant: Optional[HomeAssistant] = None,
    ) -> None:
        self._settings_path = settings_path
        self._make_speaker = make_speaker
        self._bind_address = bind_address
        self._client_factory = client_factory
        self._port_is_free = port_is_free
        self._home_assistant = home_assistant or HomeAssistant()

        self._settings = settings.Settings()
        self._unsupported_reason = ""  # why MA support is off, if it is
        self._connection: Optional["MusicAssistantConnection"] = None
        self._is_qobuz_logged_in = False
        self._speakers: dict[str, Any] = {}
        self._running_configs: dict[str, SpeakerConfig] = {}
        self._parties: dict[str, "Party"] = {}  # of the running party speakers
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
        if name and slugify_name(name) in self._names_in_use(except_key=player_id):
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

    async def add_party_speaker(self, **fields: Any) -> str:
        """Add a party speaker to the Qobuz app; returns its party id.

        ``fields``: any of PARTY_FIELDS; the others get their defaults.

        Raises:
            ValueError: with a message for the web UI.
        """
        party_id = secrets.token_hex(4)
        while party_id in self._settings.party_speakers:
            party_id = secrets.token_hex(4)
        defaults = asdict(PartySpeaker(name="", main_player_id="", http_port=0))
        values = self._checked_party_speaker(PARTY + party_id, defaults, fields)
        party = PartySpeaker(**values, http_port=self._free_port())
        self._settings.party_speakers[party_id] = party
        self._save()
        logger.info(f"Added the party speaker {party.name}")
        await self.update_speakers()
        return party_id

    async def update_party_speaker(self, party_id: str, **fields: Any) -> None:
        """Change some of a party speaker's PARTY_FIELDS; its speaker restarts once idle.

        Raises:
            ValueError: with a message for the web UI.
        """
        current = self._settings.party_speakers.get(party_id)
        if current is None:
            raise ValueError("That party speaker is not there any more.")
        values = self._checked_party_speaker(PARTY + party_id, asdict(current), fields)
        party = PartySpeaker(**values, http_port=current.http_port)
        self._settings.party_speakers[party_id] = party
        self._save()
        logger.info(f"Changed the party speaker {party.name}")
        await self.update_speakers()

    async def remove_party_speaker(self, party_id: str) -> None:
        """Take a party speaker out of the Qobuz app for good.

        Raises:
            ValueError: with a message for the web UI.
        """
        party = self._settings.party_speakers.pop(party_id, None)
        if party is None:
            raise ValueError("That party speaker is not there any more.")
        self._settings.sessions.pop(PARTY + party_id, None)
        self._save()
        logger.info(f"Removed the party speaker {party.name}")
        await self.update_speakers()

    async def home_assistant_players(self) -> list[dict[str, Any]]:
        """Home Assistant's media players, for the party speaker form.

        Raises:
            HomeAssistantError: with a message for the web UI, also outside Home Assistant.
        """
        players = await self._home_assistant.media_players()
        return [
            {"entity_id": p.entity_id, "name": p.name, "can_group": p.can_group}
            for p in sorted(players, key=lambda p: (p.name.lower(), p.entity_id))
        ]

    def _checked_party_speaker(
        self, key: str, current: dict[str, Any], fields: dict[str, Any]
    ) -> dict[str, Any]:
        """``current`` with ``fields`` applied, checked; every PARTY_FIELDS value but the port.

        Raises:
            ValueError: with a message for the web UI.
        """
        for field_name, value in fields.items():
            kind = PARTY_FIELDS.get(field_name)
            if (
                kind is None
                or not isinstance(value, kind)
                or (isinstance(value, list) and not all(isinstance(v, str) for v in value))
            ):
                raise ValueError(f"Invalid field: {field_name}"[:100])
        merged = {**current, **fields}
        values = {field_name: merged[field_name] for field_name in PARTY_FIELDS}
        name = values["name"] = " ".join(values["name"].split())
        if not name:
            raise ValueError("Give the party speaker a name.")
        if len(name) > NAME_MAX_LENGTH:
            raise ValueError(f"Keep the name to {NAME_MAX_LENGTH} characters.")
        if not name.isprintable():
            raise ValueError("The name can only have letters, numbers, spaces and punctuation.")
        if slugify_name(name) in self._names_in_use(except_key=key):
            raise ValueError(f"Another speaker is already called “{name}”.")
        main_id = values["main_player_id"]
        if not main_id:
            raise ValueError("Pick the speaker that plays the music.")
        if main_id not in self._settings.known_players:
            raise ValueError("That Music Assistant player is not known (any more).")
        for field_name, allowed, problem in [
            ("grouping", GROUPINGS, "Pick who groups the speakers."),
            ("busy_speakers", BUSY_SPEAKERS, "Pick what happens to speakers already playing."),
            ("after_party", AFTER_PARTY, "Pick what happens when the music stops."),
            ("on_leave", ON_LEAVE, "Pick what happens when you switch to another device."),
        ]:
            if values[field_name] not in allowed:
                raise ValueError(problem)
        members = values["member_ids"] = list(dict.fromkeys(values["member_ids"]))
        if not members:
            raise ValueError("Pick at least one speaker to join the party.")
        if values["grouping"] == "home_assistant":
            self._check_home_assistant_members(values["main_entity_id"], members)
        else:
            values["main_entity_id"] = ""
            self._check_music_assistant_members(main_id, members)
        return values

    def _check_music_assistant_members(self, main_id: str, member_ids: list[str]) -> None:
        if main_id in member_ids:
            raise ValueError("The main speaker can't also be one that joins.")
        if any(member_id not in self._settings.known_players for member_id in member_ids):
            raise ValueError("That Music Assistant player is not known (any more).")
        main = self._connection.player(main_id) if self._connection is not None else None
        can_group_with = main.can_group_with if main is not None else set()
        for member_id in member_ids:
            if can_group_with and member_id not in can_group_with:
                known = self._settings.known_players
                raise ValueError(
                    f"{known[member_id].name} can't sync with {known[main_id].name} in Music "
                    "Assistant."
                )

    def _check_home_assistant_members(self, main_entity_id: str, member_ids: list[str]) -> None:
        if not self._home_assistant.available:
            raise ValueError(NOT_AVAILABLE)

        def is_media_player(entity_id: str) -> bool:
            return (
                entity_id.startswith("media_player.")
                and len(entity_id) <= ENTITY_ID_MAX_LENGTH
                and entity_id.isprintable()
            )

        if not is_media_player(main_entity_id):
            raise ValueError("Pick the main speaker in Home Assistant.")
        if not all(is_media_player(entity_id) for entity_id in member_ids):
            raise ValueError("Pick Home Assistant media players to join.")
        if main_entity_id in member_ids:
            raise ValueError("The main speaker can't also be one that joins.")

    def _names_in_use(self, except_key: str) -> set[str]:
        """Slugs of every speaker name but the one of ``except_key`` (player id or party key)."""
        names = [c.name for key, c in self._wanted_speakers().items() if key != except_key]
        names += [n for key, n in self._settings.speaker_names.items() if key != except_key]
        names += [
            p.name
            for party_id, p in self._settings.party_speakers.items()
            if PARTY + party_id != except_key
        ]
        return {slugify_name(name) for name in names}

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
                    "can_group_with": self._can_group_with(player_id),
                }
                for player_id, p in sorted(known.items(), key=lambda kv: kv[1].name.lower())
            ],
            "party_speakers": [
                self._party_row(party_id, party, wanted)
                for party_id, party in sorted(
                    self._settings.party_speakers.items(), key=lambda kv: kv[1].name.lower()
                )
            ],
            "home_assistant_available": self._home_assistant.available,
            "speaker_count": len(self._speakers),
        }

    def _party_row(
        self, party_id: str, party: PartySpeaker, wanted: dict[str, SpeakerConfig]
    ) -> dict[str, Any]:
        key = PARTY + party_id
        running = self._parties.get(key)
        return {
            "party_id": party_id,
            **{k: v for k, v in asdict(party).items() if k != "http_port"},
            "speaker_name": wanted[key].name if key in wanted else "",
            "problem": running.problem if running is not None else "",
        }

    def _can_group_with(self, player_id: str) -> list[str]:
        """The players MA says this one can sync with; empty when MA does not say."""
        player = self._connection.player(player_id) if self._connection is not None else None
        return sorted(player.can_group_with) if player is not None else []

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
        """Start, stop or restart speakers to match the exposed players and the party speakers."""
        async with self._lock:
            self._update_known_players()
            ready = self._is_qobuz_logged_in and self._settings.is_configured and self._connection
            wanted = self._wanted_speakers() if ready else {}

            for key, running in list(self._running_configs.items()):
                want = wanted.get(key)
                if (
                    want is not None
                    and (want.name, want.http_port) == (running.name, running.http_port)
                    and not self._party_changed(key)
                ):
                    continue
                if want is not None and self._is_playing(key):
                    continue  # a rename restarts the speaker: wait until it is idle
                await self._stop_speaker(key)
            for key in list(self._retry_tasks):
                if key not in wanted:
                    self._retry_tasks.pop(key).cancel()

            new = [
                key for key in wanted if key not in self._speakers and key not in self._retry_tasks
            ]
            started = await asyncio.gather(*(self._start_speaker(key, wanted[key]) for key in new))
            for key, ok in zip(new, started):
                if not ok:
                    self._retry_tasks[key] = asyncio.create_task(
                        self._retry_start(key, wanted[key])
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
        """Speaker configs for the players to expose and the party speakers, with unique names.

        A party speaker is wanted while its main player is known.
        """
        known = self._settings.known_players
        if self._settings.expose_all:
            exposed = [player_id for player_id, p in known.items() if not p.hidden]
        else:
            exposed = [
                player_id for player_id in self._settings.selected_player_ids if player_id in known
            ]
        wanted = [
            (
                player_id,
                self._settings.speaker_names.get(player_id) or known[player_id].name + NAME_SUFFIX,
                known[player_id].http_port,
            )
            for player_id in sorted(exposed)
        ]
        wanted += [
            (PARTY + party_id, party.name, party.http_port)
            for party_id, party in sorted(self._settings.party_speakers.items())
            if party.main_player_id in known
        ]
        used: set[str] = set()
        configs = {}
        for key, base, port in wanted:
            name, n = base, 2
            while slugify_name(name) in used:
                name, n = f"{base} {n}", n + 1
            used.add(slugify_name(name))
            configs[key] = SpeakerConfig(
                name=name,
                uuid=speaker_uuid(key),
                backend_type="music_assistant",
                max_quality=27,  # MA streams with its own quality; this only checks availability
                http_port=port,
                bind_address=self._bind_address,
            )
        return configs

    def _free_port(self) -> int:
        taken = {p.http_port for p in self._settings.known_players.values()}
        taken |= {p.http_port for p in self._settings.party_speakers.values()}
        port = FIRST_PORT
        while port in taken or not self._port_is_free(self._bind_address, port):
            port += 1
        return port

    async def _start_speaker(self, key: str, config: SpeakerConfig) -> bool:
        from .backend import MusicAssistantBackend
        from .party import Party, PartyBackend

        assert self._connection is not None  # no speaker is wanted before start() made it
        session = self._settings.sessions.get(key)

        def save_session(session: Optional[dict[str, Any]]) -> None:
            self._save_session(key, session)

        party_speaker = self._party_speaker(key)
        party = None
        backend: MusicAssistantBackend
        if party_speaker is None:
            backend = MusicAssistantBackend(
                self._connection, key, config.name, session=session, save_session=save_session
            )
        else:
            party = Party(party_speaker, self._connection, self._home_assistant, config.name)
            backend = PartyBackend(
                self._connection, party, config.name, session=session, save_session=save_session
            )
        speaker = self._make_speaker(config, backend)
        if not await speaker.start():
            return False
        self._speakers[key] = speaker
        self._running_configs[key] = config
        if party is not None:
            self._parties[key] = party
        return True

    def _party_speaker(self, key: str) -> Optional[PartySpeaker]:
        if not key.startswith(PARTY):
            return None
        return self._settings.party_speakers.get(key[len(PARTY) :])

    def _party_changed(self, key: str) -> bool:
        """Whether a running party speaker's settings changed since it started."""
        party = self._parties.get(key)
        return party is not None and party.party_speaker != self._party_speaker(key)

    async def _retry_start(self, key: str, config: SpeakerConfig) -> None:
        """Keep trying to start a speaker that failed to start (e.g. its port was taken)."""
        try:
            for attempt in itertools.count():
                delay = START_RETRY_DELAYS_SECONDS[
                    min(attempt, len(START_RETRY_DELAYS_SECONDS) - 1)
                ]
                logger.info(f"[{config.name}] Retrying in {delay:.0f}s")
                await asyncio.sleep(delay)
                async with self._lock:
                    if await self._start_speaker(key, config):
                        return
        finally:
            if self._retry_tasks.get(key) is asyncio.current_task():
                del self._retry_tasks[key]

    def _is_playing(self, key: str) -> bool:
        return self._speakers[key].get_status()["status"] in ("playing", "paused")

    async def _stop_speaker(self, key: str) -> None:
        self._running_configs.pop(key)
        self._parties.pop(key, None)
        await self._speakers.pop(key).stop()

    async def _stop_all_speakers(self) -> None:
        async with self._lock:
            for task in self._retry_tasks.values():
                task.cancel()
            self._retry_tasks.clear()
            for key in list(self._speakers):
                await self._stop_speaker(key)
            self._logged_speaker_names = []

    def _save_session(self, key: str, session: Optional[dict[str, Any]]) -> None:
        sessions = self._settings.sessions
        if sessions.get(key) == session:
            return
        if session is None:
            del sessions[key]
        else:
            sessions[key] = session
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
