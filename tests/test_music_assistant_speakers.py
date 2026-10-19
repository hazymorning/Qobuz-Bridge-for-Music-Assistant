"""MusicAssistantSpeakers: which MA players become Qobuz Connect devices, and how they stay stable.

The music_assistant runs on the real MusicAssistantConnection against the in-memory FakeMusicAssistantServer (real
music-assistant-client underneath); the speakers are stubs that record their SpeakerConfig and
start/stop calls.
"""

from __future__ import annotations

import asyncio
import json
import uuid
from typing import Any, Callable

import pytest

pytest.importorskip("music_assistant_models")
pytest.importorskip("music_assistant_client")

from music_assistant_models.enums import PlayerType  # noqa: E402

from qobuz_proxy.backends.music_assistant import LoginError, settings  # noqa: E402
from qobuz_proxy.backends.music_assistant import connection as connection_module  # noqa: E402
from qobuz_proxy.backends.music_assistant import speakers as manager_module  # noqa: E402
from qobuz_proxy.backends.music_assistant.backend import MusicAssistantBackend  # noqa: E402
from qobuz_proxy.backends.music_assistant.connection import ConnectionState  # noqa: E402
from qobuz_proxy.backends.music_assistant.speakers import (  # noqa: E402
    FIRST_PORT,
    MusicAssistantSpeakers,
    speaker_uuid,
)
from qobuz_proxy.backends.music_assistant.settings import KnownPlayer, Settings  # noqa: E402
from qobuz_proxy.config import _SPEAKER_UUID_NAMESPACE, SpeakerConfig  # noqa: E402
from tests.backends.fake_music_assistant import FakeMusicAssistantServer  # noqa: E402

SHAREABLE = {"up1", "grp", "pair"}  # player, group, stereo pair (not hidden/disabled/protocol)
BIND = "192.0.2.10"


@pytest.fixture(autouse=True)
def fast_timers(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(manager_module, "UPDATE_DELAY_SECONDS", 0.01)
    monkeypatch.setattr(manager_module, "START_RETRY_DELAYS_SECONDS", (0.05,))
    monkeypatch.setattr(connection_module, "RECONNECT_DELAYS_SECONDS", (0.02,))


@pytest.fixture
def server(monkeypatch: pytest.MonkeyPatch) -> FakeMusicAssistantServer:
    server = FakeMusicAssistantServer()
    server.add_qobuz_provider()
    server.add_player("up1", "Kitchen")
    server.add_player("grp", "Downstairs", type=PlayerType.GROUP, group_members=["up1"])
    server.add_player("pair", "Bedroom pair", type=PlayerType.STEREO_PAIR)
    server.add_player("hid", "Hidden speaker", hide_in_ui=True)
    server.add_player("dis", "Disabled speaker", enabled=False)
    server.add_player("proto", "AirPlay protocol", type=PlayerType.PROTOCOL)
    # The login helper talks to the fake server too
    monkeypatch.setattr(connection_module, "login", server.login)
    monkeypatch.setattr(connection_module, "MusicAssistantClient", server.client_class)
    return server


class StubSpeaker:
    def __init__(
        self, config: SpeakerConfig, backend: MusicAssistantBackend, factory: SpeakerFactory
    ) -> None:
        self.config = config
        self.backend = backend
        self.player_id = backend.player_id
        self.factory = factory
        self.running = False
        self.status = "idle"

    async def start(self) -> bool:
        self.factory.log.append(("start", self.player_id))
        if self.factory.failures.get(self.player_id, 0) > 0:
            self.factory.failures[self.player_id] -= 1
            return False
        self.running = True
        return True

    async def stop(self) -> None:
        self.factory.log.append(("stop", self.player_id))
        self.running = False

    def get_status(self) -> dict[str, Any]:
        return {"status": self.status}


class SpeakerFactory:
    """make_speaker(config, backend) that records every speaker it builds."""

    def __init__(self) -> None:
        self.created: list[StubSpeaker] = []
        self.log: list[tuple[str, str]] = []
        self.failures: dict[str, int] = {}  # player id -> number of failed starts

    def __call__(self, config: SpeakerConfig, backend: MusicAssistantBackend) -> StubSpeaker:
        speaker = StubSpeaker(config, backend, self)
        self.created.append(speaker)
        return speaker

    @property
    def running(self) -> dict[str, StubSpeaker]:
        result: dict[str, StubSpeaker] = {}
        for speaker in self.created:
            if speaker.running:
                assert speaker.player_id not in result, f"two devices for {speaker.player_id}"
                result[speaker.player_id] = speaker
        return result

    def names(self) -> dict[str, str]:
        return {player_id: s.config.name for player_id, s in self.running.items()}

    def starts(self, player_id: str) -> int:
        return self.log.count(("start", player_id))

    def stops(self, player_id: str) -> int:
        return self.log.count(("stop", player_id))


class Harness:
    def __init__(self, server: FakeMusicAssistantServer, tmp_path: Any) -> None:
        self.server = server
        self.path = tmp_path / "music_assistant.json"
        self.factory = SpeakerFactory()
        self.busy_ports: set[int] = set()
        self.managers: list[MusicAssistantSpeakers] = []

    def configure(self, **kwargs: Any) -> None:
        """Settings as saved by an earlier run (connected with the server's token)."""
        saved = Settings(url=self.server.url, token=self.server.token, username="admin", **kwargs)
        settings.save(self.path, saved)

    def saved(self) -> Settings:
        return settings.load(self.path)

    def port_is_free(self, address: str, port: int) -> bool:
        assert address == BIND
        return port not in self.busy_ports

    async def started(self, *, qobuz_ready: bool = True) -> MusicAssistantSpeakers:
        music_assistant = MusicAssistantSpeakers(
            self.path,
            self.factory,
            bind_address=BIND,
            client_factory=self.server.client_factory,
            port_is_free=self.port_is_free,
        )
        self.managers.append(music_assistant)
        await music_assistant.start()
        if qobuz_ready:
            await music_assistant.set_qobuz_logged_in(True)
        return music_assistant

    async def wait_for(self, cond: Callable[[], Any], what: str, timeout: float = 3.0) -> None:
        loop = asyncio.get_running_loop()
        deadline = loop.time() + timeout
        while True:
            await self.server.settle()
            if cond():
                return
            if loop.time() > deadline:
                pytest.fail(f"timed out waiting for {what}")
            await asyncio.sleep(0.005)

    async def all_started(self) -> None:
        await self.wait_for(lambda: set(self.factory.running) == SHAREABLE, "devices started")

    async def idle(self, seconds: float) -> None:
        loop = asyncio.get_running_loop()
        deadline = loop.time() + seconds
        while loop.time() < deadline:
            await self.server.settle()
            await asyncio.sleep(0.01)

    async def close(self) -> None:
        for music_assistant in self.managers:
            await music_assistant.stop()


@pytest.fixture
async def h(server: FakeMusicAssistantServer, tmp_path: Any):
    harness = Harness(server, tmp_path)
    try:
        yield harness
    finally:
        await harness.close()


def players_by_id(status: dict[str, Any]) -> dict[str, dict[str, Any]]:
    return {p["player_id"]: p for p in status["players"]}


# =============================================================================
# Which players are shared
# =============================================================================


class TestExposure:
    async def test_expose_all_shares_players_groups_and_stereo_pairs(self, h):
        h.configure()
        music_assistant = await h.started()
        await h.all_started()

        assert h.factory.names() == {
            "up1": "Kitchen (MA)",
            "grp": "Downstairs (MA)",
            "pair": "Bedroom pair (MA)",
        }
        config = h.factory.running["grp"].config
        assert config.backend_type == "music_assistant"
        assert config.max_quality == 27
        assert config.bind_address == BIND
        # One shared MA connection for every speaker
        assert all(s.backend._connection is music_assistant.connection for s in h.factory.created)

    async def test_hidden_disabled_and_protocol_players_are_not_shared(self, h):
        h.configure()
        music_assistant = await h.started()
        await h.all_started()
        await h.idle(0.05)

        players = players_by_id(music_assistant.status())
        assert set(players) == {"up1", "grp", "pair", "hid"}  # disabled/protocol: not listed
        assert players["hid"]["hidden"] is True and players["hid"]["speaker_name"] == ""
        assert {player_id for player_id, p in players.items() if p["speaker_name"]} == SHAREABLE
        assert players["grp"]["type"] == "group" and players["pair"]["type"] == "stereo_pair"

    async def test_selection_mode_shares_only_the_ticked_players_and_is_saved(self, h, server):
        h.configure()
        music_assistant = await h.started()
        await h.all_started()

        await music_assistant.set_exposed_players(expose_all=False, selected=["grp", "hid", "grp"])

        assert set(h.factory.running) == {"grp", "hid"}  # ticking a hidden player shares it
        assert h.factory.stops("up1") == 1 and h.factory.stops("pair") == 1
        assert h.factory.starts("grp") == 1  # kept running, not restarted
        saved = h.saved()
        assert saved.expose_all is False
        assert saved.selected_player_ids == ["grp", "hid"]
        assert saved.token == server.token  # untouched
        players = players_by_id(music_assistant.status())
        assert players["grp"]["selected"] and not players["up1"]["selected"]

    async def test_duplicate_names_get_a_number(self, h, server):
        server.add_player("up2", "kitchen")  # same speaker name, case-insensitively
        h.configure()
        await h.started()
        await h.wait_for(lambda: len(h.factory.running) == 4, "devices started")

        assert h.factory.names() == {
            "up1": "Kitchen (MA)",
            "up2": "kitchen (MA) 2",
            "grp": "Downstairs (MA)",
            "pair": "Bedroom pair (MA)",
        }

    async def test_devices_start_only_once_qobuz_is_logged_in(self, h):
        h.configure()
        music_assistant = await h.started(qobuz_ready=False)
        await h.wait_for(lambda: music_assistant.connection.is_connected, "MA connected")
        await h.idle(0.1)
        assert h.factory.created == []
        assert music_assistant.status()["speaker_count"] == 0

        await music_assistant.set_qobuz_logged_in(True)
        await h.all_started()


# =============================================================================
# Stable identity: UUIDs and discovery ports
# =============================================================================


class TestIdentity:
    async def test_uuid_comes_from_the_player_id_and_survives_a_rename(self, h, server):
        h.configure()
        await h.started()
        await h.wait_for(lambda: "up1" in h.factory.running, "Kitchen started")
        before = h.factory.running["up1"].config

        expected = str(uuid.uuid5(_SPEAKER_UUID_NAMESPACE, "music_assistant:up1"))
        assert before.uuid == expected == speaker_uuid("up1")
        assert len({s.config.uuid for s in h.factory.running.values()}) == len(SHAREABLE)

        server.update_player("up1", name="Cuisine")
        await h.wait_for(lambda: h.factory.names().get("up1") == "Cuisine (MA)", "renamed speaker")
        after = h.factory.running["up1"].config
        assert after.uuid == expected
        assert after.http_port == before.http_port

    async def test_ports_start_at_8700_and_skip_busy_ports(self, h):
        h.busy_ports = {FIRST_PORT + 1}
        h.configure()
        await h.started()
        await h.all_started()

        known = h.saved().known_players
        assert set(known) == {"up1", "grp", "pair", "hid"}  # incl. the hidden player, for later
        ports = sorted(kp.http_port for kp in known.values())
        assert ports == [FIRST_PORT, FIRST_PORT + 2, FIRST_PORT + 3, FIRST_PORT + 4]
        assert {s.config.http_port for s in h.factory.running.values()} <= set(ports)

    async def test_ports_are_remembered_and_reused_after_a_restart(self, h, server):
        h.configure()
        first = await h.started()
        await h.all_started()
        ports = {player_id: s.config.http_port for player_id, s in h.factory.running.items()}
        await first.stop()
        assert all(not s.running for s in h.factory.created)

        # Next start: those ports are bound by someone else for a moment, and a new player
        # appeared in MA
        h.busy_ports = set(ports.values())
        server.add_player("up3", "Bathroom")
        h.factory.created.clear()
        await h.started()
        await h.wait_for(lambda: len(h.factory.running) == 4, "devices started")

        again = {player_id: s.config.http_port for player_id, s in h.factory.running.items()}
        assert {player_id: again[player_id] for player_id in ports} == ports
        known_ports = [kp.http_port for kp in h.saved().known_players.values()]
        assert known_ports.count(again["up3"]) == 1
        assert again["up3"] >= FIRST_PORT and again["up3"] not in h.busy_ports

    async def test_sessions_are_saved_per_player_and_handed_back_after_a_restart(self, h):
        h.configure()
        first = await h.started()
        await h.all_started()
        session = {"current": {"item_id": "qi1", "track_id": "101"}, "armed": None}

        h.factory.running["up1"].backend._save_session_callback(session)
        assert h.saved().sessions == {"up1": session}
        await first.stop()
        h.factory.created.clear()
        await h.started()
        await h.all_started()

        assert h.factory.running["up1"].backend._saved_session == session
        assert h.factory.running["grp"].backend._saved_session is None
        h.factory.running["up1"].backend._save_session_callback(None)
        assert h.saved().sessions == {}


# =============================================================================
# Music Assistant offline, players coming and going
# =============================================================================


class TestOffline:
    async def test_known_players_are_shared_while_ma_is_down(self, h, server):
        server.reachable = False
        h.configure(
            known_players={
                "up1": KnownPlayer("Kitchen", "player", 8700),
                "grp": KnownPlayer("Downstairs", "group", 8701),
                "hid": KnownPlayer("Hidden speaker", "player", 8702, hidden=True),
            }
        )
        music_assistant = await h.started()

        assert h.factory.names() == {"up1": "Kitchen (MA)", "grp": "Downstairs (MA)"}
        assert {player_id: s.config.http_port for player_id, s in h.factory.running.items()} == {
            "up1": 8700,
            "grp": 8701,
        }
        await h.idle(0.3)  # many failed reconnects later
        assert not music_assistant.connection.is_connected
        assert set(h.factory.running) == {"up1", "grp"}
        assert h.factory.stops("up1") == 0 and len(h.factory.created) == 2
        kitchen = players_by_id(music_assistant.status())["up1"]
        assert kitchen["available"] is False and kitchen["speaker_name"] == "Kitchen (MA)"

        server.reachable = True  # MA is back: the same devices stay, the rest join
        await h.all_started()
        assert h.factory.starts("up1") == 1

    async def test_player_removed_from_ma_is_kept_for_the_grace_period(
        self, h, server, monkeypatch
    ):
        monkeypatch.setattr(manager_module, "MISSING_PLAYER_GRACE_SECONDS", 0.3)
        h.configure(sessions={"pair": {"current": None, "armed": None}})
        music_assistant = await h.started()
        await h.all_started()

        server.remove_player("pair")
        await h.idle(0.1)
        assert "pair" in h.factory.running  # MA may just be re-registering it
        assert players_by_id(music_assistant.status())["pair"]["available"] is False

        await h.idle(0.3)
        server.set_player_volume("up1", 25)  # any later MA event re-checks the players
        await h.wait_for(lambda: "pair" not in h.factory.running, "speaker removed")
        saved = h.saved()
        assert "pair" not in saved.known_players and "pair" not in saved.sessions
        assert "pair" not in players_by_id(music_assistant.status())


# =============================================================================
# Speaker lifecycle
# =============================================================================


class TestLifecycle:
    async def test_rename_while_the_device_plays_waits_until_it_is_idle(self, h, server):
        h.configure()
        await h.started()
        await h.all_started()
        kitchen = h.factory.running["up1"]
        kitchen.status = "playing"

        server.update_player("up1", name="Cuisine")
        await h.idle(0.2)
        assert h.factory.running["up1"] is kitchen  # not restarted mid-song
        assert kitchen.config.name == "Kitchen (MA)" and h.factory.stops("up1") == 0

        kitchen.status = "idle"
        server.set_player_volume("grp", 20)  # the next MA event lets it catch up
        await h.wait_for(lambda: h.factory.names().get("up1") == "Cuisine (MA)", "renamed speaker")
        assert h.factory.stops("up1") == 1

    async def test_qobuz_logout_stops_every_device_and_login_starts_them(self, h):
        h.configure()
        music_assistant = await h.started()
        await h.all_started()

        await music_assistant.set_qobuz_logged_in(False)
        assert h.factory.running == {}
        assert music_assistant.status()["speaker_count"] == 0

        await music_assistant.set_qobuz_logged_in(True)
        assert set(h.factory.running) == SHAREABLE
        assert len(h.factory.created) == 2 * len(SHAREABLE)

    async def test_restart_recreates_the_running_devices(self, h):
        h.configure()
        music_assistant = await h.started()
        await h.all_started()
        old = dict(h.factory.running)

        await music_assistant.set_qobuz_logged_in(True, restart=True)

        assert set(h.factory.running) == SHAREABLE
        assert all(h.factory.running[player_id] is not old[player_id] for player_id in old)
        assert all(not s.running for s in old.values())

    async def test_failed_start_is_retried_and_never_duplicated(self, h, server, monkeypatch):
        monkeypatch.setattr(manager_module, "START_RETRY_DELAYS_SECONDS", (0.3, 0.05))
        h.factory.failures["up1"] = 2
        h.configure()
        music_assistant = await h.started()
        await h.wait_for(lambda: h.factory.starts("up1") == 1, "first attempt")
        await h.idle(0.05)
        assert music_assistant.status()["speaker_count"] == 2  # Kitchen waits for its retry

        # MA events while the retry waits must not start a second speaker
        for level in (11, 12, 13):
            server.set_player_volume("grp", level)
            await h.idle(0.03)
        await music_assistant.update_speakers()
        assert h.factory.starts("up1") == 1

        await h.wait_for(lambda: "up1" in h.factory.running, "speaker started on retry")
        await h.idle(0.2)
        assert h.factory.starts("up1") == 3  # two failures, then success
        assert len([s for s in h.factory.created if s.player_id == "up1"]) == 3
        assert music_assistant.status()["speaker_count"] == 3


# =============================================================================
# Connecting to Music Assistant from the web UI
# =============================================================================


class TestConnect:
    async def test_login_saves_a_new_token_connects_and_starts_the_devices(self, h, server):
        music_assistant = await h.started()
        assert music_assistant.connection.status.state == ConnectionState.NOT_CONFIGURED

        await music_assistant.connect(server.url, "admin", "admin-password")

        saved = h.saved()
        assert saved.url == server.url and saved.username == "admin"
        assert saved.token and saved.token != server.token  # a new long-lived token
        assert saved.token in server.valid_tokens
        # connect() returns once MA is connected: the web UI shows this very status
        status = music_assistant.status()
        assert status["is_configured"] is True
        assert status["connection"]["state"] == "connected"
        assert status["connection"]["username"] == "admin"
        assert set(h.factory.running) == SHAREABLE
        assert saved.token not in json.dumps(status)

    @pytest.mark.parametrize("typed", ["127.0.0.1:8095/", "  http://127.0.0.1:8095//  ", ""])
    async def test_typed_address_is_normalised(self, h, server, typed):
        music_assistant = await h.started()
        await music_assistant.connect(typed, "admin", "admin-password")
        assert h.saved().url == "http://127.0.0.1:8095" == server.url

    @pytest.mark.parametrize(
        ("case", "expected"),
        [
            ("wrong_password", "did not accept that username and password"),
            ("unreachable", "Can't reach Music Assistant at http://127.0.0.1:8095"),
            ("setup_required", "Music Assistant is not set up yet"),
            ("guest", "is a guest"),
            ("missing_password", "Enter your Music Assistant username and password"),
        ],
    )
    async def test_login_problems_come_back_as_plain_messages(self, h, server, case, expected):
        music_assistant = await h.started()
        server.users["visitor"] = "visitor-password"
        server.guest_users.add("visitor")
        username, password = "admin", "admin-password"
        if case == "wrong_password":
            password = "nope-typo"
        elif case == "unreachable":
            server.reachable = False
        elif case == "setup_required":
            server.setup_required = True
        elif case == "guest":
            username, password = "visitor", "visitor-password"
        elif case == "missing_password":
            password = ""

        with pytest.raises(LoginError) as err:
            await music_assistant.connect(server.url, username, password)

        assert expected in str(err.value)
        assert not password or password not in str(err.value)
        assert h.saved().token == ""
        assert music_assistant.connection.status.state == ConnectionState.NOT_CONFIGURED

    async def test_disconnect_forgets_the_token_but_keeps_the_selection(self, h):
        h.configure(expose_all=False, selected_player_ids=["grp"])
        music_assistant = await h.started()
        await h.wait_for(lambda: set(h.factory.running) == {"grp"}, "speaker started")

        await music_assistant.disconnect()

        saved = h.saved()
        assert saved.token == ""
        assert saved.url == h.server.url
        assert saved.selected_player_ids == ["grp"] and saved.expose_all is False
        assert h.factory.running == {}
        assert music_assistant.connection.status.state == ConnectionState.NOT_CONFIGURED
        status = music_assistant.status()
        assert status["is_configured"] is False and status["speaker_count"] == 0


# =============================================================================
# status()
# =============================================================================


class TestStatus:
    async def test_status_lists_known_players_by_name_and_never_the_token(self, h, server):
        h.configure()
        music_assistant = await h.started()
        await h.all_started()

        status = music_assistant.status()

        assert status == {
            "is_configured": True,
            "connection": {
                "state": "connected",
                "message": status["connection"]["message"],
                "server_version": "2.10.7",
                "has_qobuz_provider": True,
                "url": server.url,
                "username": "admin",
            },
            "expose_all": True,
            "players": status["players"],
            "speaker_count": len(SHAREABLE),
        }
        assert [p["name"] for p in status["players"]] == [
            "Bedroom pair",
            "Downstairs",
            "Hidden speaker",
            "Kitchen",
        ]
        assert players_by_id(status)["up1"] == {
            "player_id": "up1",
            "name": "Kitchen",
            "type": "player",
            "available": True,
            "hidden": False,
            "selected": False,
            "speaker_name": "Kitchen (MA)",
        }
        assert server.token not in json.dumps(status)
