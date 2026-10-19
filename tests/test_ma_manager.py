"""MusicAssistantManager: which MA players become Qobuz Connect devices, and how they stay stable.

The manager runs on the real MusicAssistantConnection against the in-memory FakeMAServer (real
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

from qobuz_proxy.backends.music_assistant import connection as connection_module  # noqa: E402
from qobuz_proxy.backends.music_assistant import manager as manager_module  # noqa: E402
from qobuz_proxy.backends.music_assistant.connection import (  # noqa: E402
    MAConnectionState,
    MusicAssistantConnection,
)
from qobuz_proxy.backends.music_assistant.manager import (  # noqa: E402
    MusicAssistantManager,
    ma_device_uuid,
)
from qobuz_proxy.backends.music_assistant.settings import (  # noqa: E402
    DEFAULT_MA_URL,
    MA_PORT_RANGE_START,
    KnownPlayer,
    MASettings,
    MASettingsStore,
)
from qobuz_proxy.config import _SPEAKER_UUID_NAMESPACE, SpeakerConfig  # noqa: E402
from tests.backends.fake_music_assistant import FakeMAServer  # noqa: E402

SHAREABLE = {"up1", "grp", "pair"}  # player, group, stereo pair (not hidden/disabled/protocol)


@pytest.fixture(autouse=True)
def fast_timers(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(manager_module, "RECONCILE_DELAY_SECONDS", 0.01)
    monkeypatch.setattr(manager_module, "SPEAKER_RETRY_DELAYS_SECONDS", (0.05,))
    monkeypatch.setattr(manager_module, "SPEAKER_RETRY_STEADY_DELAY_SECONDS", 0.05)
    monkeypatch.setattr(MusicAssistantConnection, "RECONNECT_DELAYS_SECONDS", (0.02,))


@pytest.fixture
def server(monkeypatch: pytest.MonkeyPatch) -> FakeMAServer:
    server = FakeMAServer()
    server.add_qobuz_provider()
    server.add_player("up1", "Kitchen")
    server.add_player("grp", "Downstairs", type=PlayerType.GROUP, group_members=["up1"])
    server.add_player("pair", "Bedroom pair", type=PlayerType.STEREO_PAIR)
    server.add_player("hid", "Hidden speaker", hide_in_ui=True)
    server.add_player("dis", "Disabled speaker", enabled=False)
    server.add_player("proto", "AirPlay protocol", type=PlayerType.PROTOCOL)
    # The login helpers talk to the fake server too
    monkeypatch.setattr(connection_module, "login", server.login)
    monkeypatch.setattr(connection_module, "MusicAssistantClient", server.client_class)
    return server


class StubSpeaker:
    def __init__(self, config: SpeakerConfig, connection: Any, factory: SpeakerFactory) -> None:
        self.config = config
        self.connection = connection
        self.factory = factory
        self.running = False
        self.status = "idle"

    @property
    def pid(self) -> str:
        return self.config.ma_player_id

    async def start(self) -> bool:
        self.factory.log.append(("start", self.pid, self.config.name))
        if self.factory.failures.get(self.pid, 0) > 0:
            self.factory.failures[self.pid] -= 1
            return False
        self.running = True
        return True

    async def stop(self) -> None:
        self.factory.log.append(("stop", self.pid, self.config.name))
        self.running = False

    def get_status(self) -> dict[str, Any]:
        playing = self.status in ("playing", "paused")
        return {"status": self.status, "now_playing": {"title": "Song"} if playing else None}


class SpeakerFactory:
    """speaker_factory(config, connection) that records every speaker it builds."""

    def __init__(self) -> None:
        self.created: list[StubSpeaker] = []
        self.log: list[tuple[str, str, str]] = []
        self.failures: dict[str, int] = {}  # player id -> number of failed starts

    def __call__(self, config: SpeakerConfig, connection: Any) -> StubSpeaker:
        speaker = StubSpeaker(config, connection, self)
        self.created.append(speaker)
        return speaker

    @property
    def running(self) -> dict[str, StubSpeaker]:
        result: dict[str, StubSpeaker] = {}
        for speaker in self.created:
            if speaker.running:
                assert speaker.pid not in result, f"two devices for {speaker.pid}"
                result[speaker.pid] = speaker
        return result

    def names(self) -> dict[str, str]:
        return {pid: s.config.name for pid, s in self.running.items()}

    def starts(self, pid: str) -> int:
        return sum(1 for kind, p, _ in self.log if kind == "start" and p == pid)

    def stops(self, pid: str) -> int:
        return sum(1 for kind, p, _ in self.log if kind == "stop" and p == pid)


class Harness:
    def __init__(self, server: FakeMAServer, tmp_path: Any) -> None:
        self.server = server
        self.store = MASettingsStore(tmp_path / "music_assistant.json")
        self.factory = SpeakerFactory()
        self.yaml: list[SpeakerConfig] = []
        self.reserved: set[int] = set()
        self.busy_ports: set[int] = set()
        self.detected_url = "http://192.168.1.20:8095"
        self.detections = 0
        self.managers: list[MusicAssistantManager] = []

    def configure(self, **kwargs: Any) -> None:
        """Settings as saved by an earlier run (connected with the server's token)."""
        settings = MASettings(url=self.server.url, token=self.server.token, username="admin")
        for key, value in kwargs.items():
            setattr(settings, key, value)
        self.store.save(settings)

    async def detect(self) -> str:
        self.detections += 1
        return self.detected_url

    def connection(self, url: str, token: str) -> MusicAssistantConnection:
        return MusicAssistantConnection(url, token, client_factory=self.server.client_factory)

    def manager(self) -> MusicAssistantManager:
        manager = MusicAssistantManager(
            store=self.store,
            speaker_factory=self.factory,
            reserved_ports=lambda: set(self.reserved),
            yaml_speakers=lambda: list(self.yaml),
            bind_address="0.0.0.0",
            connection_factory=self.connection,
            url_detector=self.detect,
            port_checker=lambda address, port: port not in self.busy_ports,
        )
        self.managers.append(manager)
        return manager

    async def started(self, *, qobuz_ready: bool = True) -> MusicAssistantManager:
        manager = self.manager()
        await manager.start()
        if qobuz_ready:
            await manager.set_qobuz_ready(True)
        return manager

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

    async def idle(self, seconds: float) -> None:
        loop = asyncio.get_running_loop()
        deadline = loop.time() + seconds
        while loop.time() < deadline:
            await self.server.settle()
            await asyncio.sleep(0.01)

    async def close(self) -> None:
        for manager in self.managers:
            await manager.stop()


@pytest.fixture
async def h(server: FakeMAServer, tmp_path: Any):
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
        await h.started()
        await h.wait_for(lambda: set(h.factory.running) == SHAREABLE, "devices started")

        assert h.factory.names() == {
            "up1": "Kitchen (MA)",
            "grp": "Downstairs (MA)",
            "pair": "Bedroom pair (MA)",
        }
        config = h.factory.running["grp"].config
        assert config.backend_type == "music_assistant"
        assert config.ma_player_id == "grp"
        assert config.max_quality == 27
        assert config.bind_address == "0.0.0.0"
        assert all(s.connection is h.managers[0].connection for s in h.factory.created)

    async def test_hidden_disabled_and_protocol_players_are_not_shared(self, h):
        h.configure()
        manager = await h.started()
        await h.wait_for(lambda: set(h.factory.running) == SHAREABLE, "devices started")
        await h.idle(0.05)

        players = players_by_id(manager.status())
        assert set(players) == {"up1", "grp", "pair", "hid"}  # disabled/protocol: not listed
        assert players["hid"]["hidden"] is True
        assert players["hid"]["exposed"] is False and players["hid"]["device"] is None
        assert {pid for pid, p in players.items() if p["exposed"]} == SHAREABLE
        assert players["grp"]["type"] == "group" and players["pair"]["type"] == "stereo_pair"

    async def test_selection_mode_shares_only_the_ticked_players(self, h):
        h.configure()
        manager = await h.started()
        await h.wait_for(lambda: set(h.factory.running) == SHAREABLE, "devices started")

        await manager.update_exposure(expose_all=False, selected=["grp", "hid", "grp"])

        assert set(h.factory.running) == {"grp", "hid"}  # ticking a hidden player shares it
        assert h.factory.stops("up1") == 1 and h.factory.stops("pair") == 1
        assert h.factory.starts("grp") == 1  # kept running, not restarted
        saved = h.store.load()
        assert saved.expose_all is False
        assert saved.selected_player_ids == ["grp", "hid"]
        players = players_by_id(manager.status())
        assert players["grp"]["selected"] and not players["up1"]["selected"]

    async def test_name_suffix_can_be_turned_off(self, h):
        h.configure()
        manager = await h.started()
        await h.wait_for(lambda: set(h.factory.running) == SHAREABLE, "devices started")

        await manager.update_exposure(name_suffix=False)

        assert h.factory.names() == {
            "up1": "Kitchen",
            "grp": "Downstairs",
            "pair": "Bedroom pair",
        }
        assert h.store.load().name_suffix is False

    async def test_duplicate_names_get_a_number(self, h, server):
        server.add_player("up2", "Kitchen")
        h.yaml = [SpeakerConfig(name="DOWNSTAIRS (MA)", backend_type="dlna", dlna_ip="192.0.2.5")]
        h.configure()
        await h.started()
        await h.wait_for(lambda: len(h.factory.running) == 4, "devices started")

        assert h.factory.names() == {
            "up1": "Kitchen (MA)",
            "up2": "Kitchen (MA) 2",
            "grp": "Downstairs (MA) 2",  # the YAML speaker has that name (case-insensitively)
            "pair": "Bedroom pair (MA)",
        }

    async def test_player_configured_in_yaml_is_left_to_yaml(self, h):
        h.yaml = [SpeakerConfig(name="Office", backend_type="music_assistant", ma_player_id="up1")]
        h.configure()
        manager = await h.started()
        await h.wait_for(lambda: set(h.factory.running) == {"grp", "pair"}, "devices started")
        await h.idle(0.05)

        assert "up1" not in h.factory.running
        player = players_by_id(manager.status())["up1"]
        assert player["in_yaml"] is True and player["exposed"] is False

    async def test_devices_start_only_once_qobuz_is_logged_in(self, h):
        h.configure()
        manager = await h.started(qobuz_ready=False)
        await h.wait_for(lambda: manager.connection.is_connected, "MA connected")
        await h.idle(0.1)
        assert h.factory.created == []
        assert all(p["device"] is None for p in manager.status()["players"])

        await manager.set_qobuz_ready(True)
        await h.wait_for(lambda: set(h.factory.running) == SHAREABLE, "devices started")


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
        assert before.uuid == expected == ma_device_uuid("up1")
        assert len({s.config.uuid for s in h.factory.running.values()}) == len(SHAREABLE)

        server.update_player("up1", name="Cuisine")
        await h.wait_for(lambda: h.factory.names().get("up1") == "Cuisine (MA)", "renamed device")
        after = h.factory.running["up1"].config
        assert after.uuid == expected
        assert after.http_port == before.http_port

    async def test_ports_start_at_8700(self, h):
        h.configure()
        await h.started()
        await h.wait_for(lambda: set(h.factory.running) == SHAREABLE, "devices started")
        ports = sorted(kp.http_port for kp in h.store.load().known_players.values())
        assert ports == [8700, 8701, 8702, 8703]  # incl. the hidden player, for later

    async def test_ports_start_at_8700_and_skip_reserved_and_busy_ports(self, h):
        h.reserved = {MA_PORT_RANGE_START, MA_PORT_RANGE_START + 2}
        h.busy_ports = {MA_PORT_RANGE_START + 1}
        h.configure()
        await h.started()
        await h.wait_for(lambda: set(h.factory.running) == SHAREABLE, "devices started")

        known = h.store.load().known_players
        assert set(known) == {"up1", "grp", "pair", "hid"}
        ports = sorted(kp.http_port for kp in known.values())
        start = MA_PORT_RANGE_START
        assert ports == [start + 3, start + 4, start + 5, start + 6]
        assert {s.config.http_port for s in h.factory.running.values()} <= set(ports)

    async def test_ports_are_remembered_and_reused_after_a_restart(self, h, server):
        h.configure()
        first = await h.started()
        await h.wait_for(lambda: set(h.factory.running) == SHAREABLE, "devices started")
        ports = {pid: s.config.http_port for pid, s in h.factory.running.items()}
        await first.stop()
        assert all(not s.running for s in h.factory.created)

        # Next start: those ports are bound by someone else for a moment, and a new player
        # appeared in MA
        h.busy_ports = set(ports.values())
        server.add_player("up3", "Bathroom")
        h.factory.created.clear()
        await h.started()
        await h.wait_for(lambda: len(h.factory.running) == 4, "devices started")

        again = {pid: s.config.http_port for pid, s in h.factory.running.items()}
        assert {pid: again[pid] for pid in ports} == ports
        known_ports = {kp.http_port for kp in h.store.load().known_players.values()}
        assert again["up3"] not in known_ports - {again["up3"]}
        assert again["up3"] >= MA_PORT_RANGE_START and again["up3"] not in h.busy_ports


# =============================================================================
# Music Assistant offline, players coming and going
# =============================================================================


class TestOffline:
    def _known(self) -> dict[str, KnownPlayer]:
        return {
            "up1": KnownPlayer("up1", "Kitchen", "player", 8700),
            "grp": KnownPlayer("grp", "Downstairs", "group", 8701),
            "hid": KnownPlayer("hid", "Hidden speaker", "player", 8702, hidden=True),
        }

    async def test_known_players_are_shared_while_ma_is_down(self, h, server):
        server.reachable = False
        h.configure(known_players=self._known())
        manager = await h.started()

        assert h.factory.names() == {"up1": "Kitchen (MA)", "grp": "Downstairs (MA)"}
        assert {pid: s.config.http_port for pid, s in h.factory.running.items()} == {
            "up1": 8700,
            "grp": 8701,
        }
        await h.idle(0.3)  # many failed reconnects later
        assert manager.connection.status.state == MAConnectionState.DISCONNECTED
        assert set(h.factory.running) == {"up1", "grp"}
        assert h.factory.stops("up1") == 0 and len(h.factory.created) == 2
        players = players_by_id(manager.status())
        assert players["up1"]["available"] is False and players["up1"]["exposed"] is True

        server.reachable = True  # MA is back: the same devices stay, the rest join
        await h.wait_for(lambda: set(h.factory.running) == SHAREABLE, "all devices")
        assert h.factory.starts("up1") == 1

    async def test_player_removed_from_ma_is_kept_for_the_grace_period(
        self, h, server, monkeypatch
    ):
        monkeypatch.setattr(manager_module, "MISSING_PLAYER_GRACE_SECONDS", 0.3)
        h.configure()
        manager = await h.started()
        await h.wait_for(lambda: set(h.factory.running) == SHAREABLE, "devices started")

        server.remove_player("pair")
        await h.idle(0.1)
        assert "pair" in h.factory.running  # MA may just be re-registering it
        assert players_by_id(manager.status())["pair"]["available"] is False

        await h.idle(0.3)
        server.set_player_volume("up1", 25)  # any later MA event re-checks the players
        await h.wait_for(lambda: "pair" not in h.factory.running, "device removed")
        assert "pair" not in h.store.load().known_players
        assert "pair" not in players_by_id(manager.status())


# =============================================================================
# Device lifecycle
# =============================================================================


class TestLifecycle:
    async def test_rename_while_the_device_plays_waits_until_it_is_idle(self, h, server):
        h.configure()
        await h.started()
        await h.wait_for(lambda: set(h.factory.running) == SHAREABLE, "devices started")
        kitchen = h.factory.running["up1"]
        kitchen.status = "playing"

        server.update_player("up1", name="Cuisine")
        await h.idle(0.2)
        assert h.factory.running["up1"] is kitchen  # not restarted mid-song
        assert kitchen.config.name == "Kitchen (MA)" and h.factory.stops("up1") == 0

        kitchen.status = "idle"
        server.set_player_volume("grp", 20)  # the next MA event lets it catch up
        await h.wait_for(lambda: h.factory.names().get("up1") == "Cuisine (MA)", "renamed device")
        assert h.factory.stops("up1") == 1

    async def test_qobuz_logout_stops_every_device_and_login_starts_them(self, h):
        h.configure()
        manager = await h.started()
        await h.wait_for(lambda: set(h.factory.running) == SHAREABLE, "devices started")

        await manager.set_qobuz_ready(False)
        assert h.factory.running == {}
        assert manager.status()["exposed_count"] == 0

        await manager.set_qobuz_ready(True)
        assert set(h.factory.running) == SHAREABLE
        assert len(h.factory.created) == 2 * len(SHAREABLE)

    async def test_restart_recreates_the_running_devices(self, h):
        h.configure()
        manager = await h.started()
        await h.wait_for(lambda: set(h.factory.running) == SHAREABLE, "devices started")
        old = dict(h.factory.running)

        await manager.set_qobuz_ready(True, restart=True)

        assert set(h.factory.running) == SHAREABLE
        assert all(h.factory.running[pid] is not old[pid] for pid in old)
        assert all(not s.running for s in old.values())

    async def test_failed_start_is_retried_and_never_duplicated(self, h, server, monkeypatch):
        monkeypatch.setattr(manager_module, "SPEAKER_RETRY_DELAYS_SECONDS", (0.3, 0.05))
        h.factory.failures["up1"] = 2
        h.configure()
        manager = await h.started()
        await h.wait_for(lambda: h.factory.starts("up1") == 1, "first attempt")
        await h.idle(0.05)
        assert players_by_id(manager.status())["up1"]["device"] == {
            "status": "starting",
            "now_playing": None,
        }

        # MA events while the retry waits must not start a second device
        for level in (11, 12, 13):
            server.set_player_volume("grp", level)
            await h.idle(0.03)
        await manager.reconcile()
        assert h.factory.starts("up1") == 1

        await h.wait_for(lambda: "up1" in h.factory.running, "device started on retry")
        await h.idle(0.2)
        assert h.factory.starts("up1") == 3  # two failures, then success
        assert len([s for s in h.factory.created if s.pid == "up1"]) == 3
        assert players_by_id(manager.status())["up1"]["device"]["status"] == "idle"


# =============================================================================
# Connecting to Music Assistant from the web UI
# =============================================================================


class TestConnect:
    async def test_login_saves_the_token_and_connects(self, h, server):
        manager = await h.started()
        assert manager.connection.status.state == MAConnectionState.NOT_CONFIGURED

        result = await manager.connect(server.url, username="admin", password="admin-password")

        assert result == {"ok": True}
        saved = h.store.load()
        assert saved.url == server.url and saved.username == "admin"
        assert saved.token and saved.token != server.token  # a new long-lived token
        assert saved.token in server.valid_tokens
        assert saved.token in h.store.path.read_text()
        await h.wait_for(lambda: manager.connection.is_connected, "MA connected")
        status = manager.status()
        assert saved.token not in json.dumps(status)
        assert status["configured"] is True
        assert status["connection"]["state"] == "connected"
        assert status["connection"]["username"] == "admin"
        await h.wait_for(lambda: set(h.factory.running) == SHAREABLE, "devices started")

    async def test_pasted_token_is_checked_and_saved(self, h, server):
        manager = await h.started()

        result = await manager.connect(server.url, token=f"  {server.token}  ")

        assert result == {"ok": True}
        saved = h.store.load()
        assert saved.token == server.token
        assert saved.username == "admin"  # the token's own user
        await h.wait_for(lambda: manager.connection.is_connected, "MA connected")

    @pytest.mark.parametrize("how", ["password", "token"])
    async def test_connect_returns_once_music_assistant_is_connected(self, h, server, how):
        manager = await h.started()
        if how == "password":
            result = await manager.connect(server.url, username="admin", password="admin-password")
        else:
            result = await manager.connect(server.url, token=server.token)

        assert result == {"ok": True}
        # The web UI shows the status from this very response
        assert manager.status()["connection"]["state"] == "connected"
        assert set(h.factory.running) == SHAREABLE

    async def test_address_without_a_scheme_is_normalised(self, h, server):
        manager = await h.started()
        result = await manager.connect(
            "127.0.0.1:8095/", username="admin", password="admin-password"
        )
        assert result == {"ok": True}
        assert h.store.load().url == "http://127.0.0.1:8095"

    @pytest.mark.parametrize(
        ("case", "expected"),
        [
            ("wrong_password", "did not accept that username and password"),
            ("unreachable", "Can't reach Music Assistant at http://127.0.0.1:8095"),
            ("setup_required", "Music Assistant is not set up yet"),
            ("guest", "is a guest"),
            ("missing_password", "Enter your Music Assistant username and password"),
            ("bad_token", "Music Assistant did not accept that token."),
        ],
    )
    async def test_login_problems_come_back_as_plain_messages(self, h, server, case, expected):
        manager = await h.started()
        server.users["visitor"] = "visitor-password"
        server.guest_users.add("visitor")
        kwargs: dict[str, str] = {"username": "admin", "password": "admin-password"}
        if case == "wrong_password":
            kwargs["password"] = "nope"
        elif case == "unreachable":
            server.reachable = False
        elif case == "setup_required":
            server.setup_required = True
        elif case == "guest":
            kwargs = {"username": "visitor", "password": "visitor-password"}
        elif case == "missing_password":
            kwargs["password"] = ""
        elif case == "bad_token":
            kwargs = {"token": "not-a-valid-token"}

        result = await manager.connect(server.url, **kwargs)

        assert result["ok"] is False
        assert expected in result["error"]
        assert (
            "admin-password" not in result["error"] and "not-a-valid-token" not in result["error"]
        )
        assert h.store.load().token == ""
        assert manager.connection.status.state == MAConnectionState.NOT_CONFIGURED

    async def test_disconnect_forgets_the_token_but_keeps_the_selection(self, h):
        h.configure(expose_all=False, selected_player_ids=["grp"])
        manager = await h.started()
        await h.wait_for(lambda: set(h.factory.running) == {"grp"}, "device started")

        await manager.disconnect()

        saved = h.store.load()
        assert saved.token == ""
        assert saved.url == h.server.url
        assert saved.selected_player_ids == ["grp"] and saved.expose_all is False
        assert h.factory.running == {}
        assert manager.connection.status.state == MAConnectionState.NOT_CONFIGURED
        status = manager.status()
        assert status["configured"] is False and status["exposed_count"] == 0

    async def test_update_exposure_is_saved(self, h):
        h.configure()
        manager = await h.started(qobuz_ready=False)
        await manager.update_exposure(expose_all=False, selected=["pair"], name_suffix=False)

        reloaded = MASettingsStore(h.store.path).load()
        assert reloaded.expose_all is False
        assert reloaded.selected_player_ids == ["pair"]
        assert reloaded.name_suffix is False
        assert reloaded.token == h.server.token  # untouched


# =============================================================================
# status()
# =============================================================================


class TestStatus:
    async def test_status_shape(self, h, server):
        h.configure()
        manager = await h.started()
        await h.wait_for(lambda: set(h.factory.running) == SHAREABLE, "devices started")
        h.factory.running["up1"].status = "playing"

        status = manager.status()

        assert set(status) == {
            "supported",
            "support_error",
            "configured",
            "connection",
            "suggested_url",
            "expose_all",
            "name_suffix",
            "qobuz_ready",
            "players",
            "exposed_count",
        }
        assert status["supported"] is True and status["support_error"] == ""
        assert status["configured"] is True and status["qobuz_ready"] is True
        assert status["expose_all"] is True and status["name_suffix"] is True
        assert status["suggested_url"] == server.url
        assert status["exposed_count"] == len(SHAREABLE)
        assert status["connection"] == {
            "state": "connected",
            "message": status["connection"]["message"],
            "url": server.url,
            "username": "admin",
            "server_version": "2.10.7",
            "qobuz_provider": True,
        }
        kitchen = players_by_id(status)["up1"]
        assert kitchen == {
            "player_id": "up1",
            "ma_name": "Kitchen",
            "qobuz_name": "Kitchen (MA)",
            "type": "player",
            "available": True,
            "hidden": False,
            "selected": False,
            "exposed": True,
            "in_yaml": False,
            "device": {"status": "playing", "now_playing": {"title": "Song"}},
        }
        names = [p["ma_name"] for p in status["players"]]
        assert names == sorted(names, key=str.lower)
        assert server.token not in json.dumps(status)

    async def test_address_detection_runs_once_when_the_panel_first_asks(self, h):
        manager = await h.started(qobuz_ready=False)
        await h.idle(0.05)
        assert h.detections == 0  # nothing probed at start-up

        first = manager.status()
        assert first["suggested_url"] == DEFAULT_MA_URL  # until the detection answers
        await h.wait_for(lambda: h.detections == 1, "detection")
        await h.idle(0.02)
        for _ in range(3):
            assert manager.status()["suggested_url"] == h.detected_url
        assert h.detections == 1

    async def test_no_address_detection_once_configured(self, h, server):
        h.configure()
        manager = await h.started(qobuz_ready=False)
        for _ in range(3):
            assert manager.status()["suggested_url"] == server.url
        await h.idle(0.05)
        assert h.detections == 0
