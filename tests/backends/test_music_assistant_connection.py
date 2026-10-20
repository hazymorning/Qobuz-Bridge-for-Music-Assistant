"""MusicAssistantConnection and the login helper, against the fake MA server.

The fake runs the real ``music-assistant-client``; only the websocket is swapped.
Reconnect and load timeouts are patched to tiny values, and waits are bounded polls.
"""

import asyncio
import logging
from collections.abc import AsyncIterator
from typing import Any, Callable

import aiohttp
import pytest

pytest.importorskip("music_assistant_models")
pytest.importorskip("music_assistant_client")

from music_assistant_client.exceptions import (  # noqa: E402
    ConnectionClosed,
    ConnectionFailed,
    InvalidState,
    NotConnected,
)
from music_assistant_models.enums import EventType, PlaybackState, PlayerType  # noqa: E402
from music_assistant_models.errors import MediaNotFoundError  # noqa: E402

from qobuz_proxy.backends.music_assistant import LoginError  # noqa: E402
from qobuz_proxy.backends.music_assistant import connection as connection_module  # noqa: E402
from qobuz_proxy.backends.music_assistant.connection import (  # noqa: E402
    ConnectionState,
    ConnectionStatus,
    MusicAssistantConnection,
    NotConnectedError,
    PlayerInfo,
    login_and_create_token,
)
from tests.fake_music_assistant import DROP, DROP_HARD, HANG, FakeMusicAssistantServer  # noqa: E402

QID = "up1"
REAL_RECONNECT_DELAYS = connection_module.RECONNECT_DELAYS_SECONDS  # before fast_timing


@pytest.fixture(autouse=True)
def fast_timing(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(connection_module, "RECONNECT_DELAYS_SECONDS", (0.01,))
    monkeypatch.setattr(connection_module, "CONNECT_TIMEOUT_SECONDS", 1.0)
    monkeypatch.setattr(connection_module, "LOAD_TIMEOUT_SECONDS", 1.0)


@pytest.fixture
def server() -> FakeMusicAssistantServer:
    srv = FakeMusicAssistantServer()
    srv.add_qobuz_provider()
    srv.add_player(QID, "Kitchen")
    return srv


@pytest.fixture
async def conn(server: FakeMusicAssistantServer) -> AsyncIterator[MusicAssistantConnection]:
    connection = MusicAssistantConnection(
        server.url, server.token, client_factory=server.client_factory
    )
    yield connection
    await connection.stop()


@pytest.fixture
def login_server(
    server: FakeMusicAssistantServer, monkeypatch: pytest.MonkeyPatch
) -> FakeMusicAssistantServer:
    """Point the login helper at the fake server."""
    monkeypatch.setattr(connection_module, "login", server.login)
    monkeypatch.setattr(connection_module, "MusicAssistantClient", server.client_class)
    return server


async def wait_for(
    cond: Callable[[], bool], server: FakeMusicAssistantServer, timeout: float = 2.0, what: str = ""
) -> None:
    """Poll ``cond`` every 5 ms (letting the fake deliver frames) until it holds."""
    loop = asyncio.get_running_loop()
    deadline = loop.time() + timeout
    while True:
        await server.settle()
        if cond():
            return
        if loop.time() > deadline:
            raise AssertionError(f"timed out waiting for {what or cond}")
        await asyncio.sleep(0.005)


async def idle(server: FakeMusicAssistantServer, seconds: float) -> None:
    """Let real time pass (for 'nothing else happens' checks)."""
    await asyncio.sleep(seconds)
    await server.settle()


def record_states(connection: MusicAssistantConnection) -> list[ConnectionStatus]:
    states: list[ConnectionStatus] = []
    connection.add_status_listener(states.append)
    return states


async def connected(conn: MusicAssistantConnection, server: FakeMusicAssistantServer) -> None:
    await conn.start()
    await wait_for(lambda: conn.is_connected, server, what="CONNECTED")


def new_client(server: FakeMusicAssistantServer, old: Any) -> bool:
    """Exactly one client is connected, and it is not ``old``."""
    return len(server.clients) == 1 and server.clients[0] is not old


class RecordedDelays(tuple):  # type: ignore[type-arg]
    """Reconnect delays that remember which one the connection picked, then wait 1 ms."""

    picked: list[float]

    def __getitem__(self, index: Any) -> Any:
        self.picked.append(super().__getitem__(index))
        return 0.001


class TestConnect:
    async def test_connects_and_reports_version_and_qobuz_provider(
        self, conn: MusicAssistantConnection, server: FakeMusicAssistantServer
    ) -> None:
        states = record_states(conn)
        await connected(conn, server)

        assert conn.status.state == ConnectionState.CONNECTED
        assert conn.status.server_version == "2.10.7"
        assert conn.status.has_qobuz_provider is True
        assert [s.state for s in states] == [ConnectionState.CONNECTING, ConnectionState.CONNECTED]

    @pytest.mark.parametrize("provider", ["missing", "unavailable"])
    async def test_has_qobuz_needs_a_working_qobuz_provider(self, provider: str) -> None:
        server = FakeMusicAssistantServer()
        if provider == "unavailable":
            server.add_qobuz_provider(available=False)
        conn = MusicAssistantConnection(
            server.url, server.token, client_factory=server.client_factory
        )
        try:
            await connected(conn, server)
            assert conn.status.has_qobuz_provider is False
        finally:
            await conn.stop()

    async def test_lists_only_enabled_players_groups_and_stereo_pairs(
        self, conn: MusicAssistantConnection, server: FakeMusicAssistantServer
    ) -> None:
        server.add_player("grp", "Downstairs", type=PlayerType.GROUP, group_members=[QID])
        server.add_player("pair", "Living Room", type=PlayerType.STEREO_PAIR)
        server.add_player("shy", "Bathroom", hide_in_ui=True)
        await connected(conn, server)
        # Added while connected: reaches the client cache through PLAYER_ADDED
        server.add_player("proto", "Kitchen (AirPlay)", type=PlayerType.PROTOCOL)
        server.add_player("off", "Garage", enabled=False)
        await wait_for(lambda: conn.player("off") is not None, server)
        assert conn.player("proto") is not None

        players = {p.player_id: p for p in conn.players()}

        assert set(players) == {QID, "grp", "pair", "shy"}
        assert players[QID] == PlayerInfo(QID, "Kitchen", "player", available=True, hidden=False)
        assert players["grp"].type == "group"
        assert players["pair"].type == "stereo_pair"
        assert players["shy"].hidden is True

    async def test_players_and_queues_come_from_the_cache(
        self, conn: MusicAssistantConnection, server: FakeMusicAssistantServer
    ) -> None:
        assert conn.players() == []
        assert conn.player(QID) is None and conn.queue(QID) is None

        await connected(conn, server)
        commands_before = len(server.command_log)
        queue = conn.queue(QID)
        assert queue is not None and queue.queue_id == QID
        player = conn.player(QID)
        assert player is not None and player.name == "Kitchen"
        assert conn.queue("nope") is None and conn.player("nope") is None
        assert len(server.command_log) == commands_before  # never calls MA

    @pytest.mark.parametrize("missing", ["url", "token"])
    async def test_without_address_or_token_it_does_not_connect(
        self, server: FakeMusicAssistantServer, missing: str
    ) -> None:
        url = "" if missing == "url" else server.url
        token = "" if missing == "token" else server.token
        conn = MusicAssistantConnection(url, token, client_factory=server.client_factory)
        await conn.start()
        await server.settle()

        assert conn.status.state == ConnectionState.NOT_CONFIGURED
        assert server.created_clients == []
        assert not conn.is_connected
        await conn.stop()


class TestReconnect:
    async def test_unreachable_music_assistant_is_retried_with_growing_backoff(
        self,
        conn: MusicAssistantConnection,
        server: FakeMusicAssistantServer,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        delays = RecordedDelays(REAL_RECONNECT_DELAYS)
        delays.picked = []
        monkeypatch.setattr(connection_module, "RECONNECT_DELAYS_SECONDS", delays)
        states = record_states(conn)
        server.reachable = False
        await conn.start()

        await wait_for(lambda: len(delays.picked) >= 9, server, what="retries")
        longest = REAL_RECONNECT_DELAYS[-1]
        assert delays.picked[:9] == [*REAL_RECONNECT_DELAYS, longest, longest]
        assert not conn.is_connected
        # Tells the user which address failed
        assert any(
            s.state == ConnectionState.DISCONNECTED and server.url in s.message for s in states
        )

        server.reachable = True
        await wait_for(lambda: conn.is_connected, server, what="CONNECTED")

    async def test_backoff_starts_over_after_a_working_connection_drops(
        self,
        conn: MusicAssistantConnection,
        server: FakeMusicAssistantServer,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        delays = RecordedDelays(REAL_RECONNECT_DELAYS)
        delays.picked = []
        monkeypatch.setattr(connection_module, "RECONNECT_DELAYS_SECONDS", delays)
        monkeypatch.setattr(connection_module, "STABLE_CONNECTION_SECONDS", 0.0)
        server.reachable = False
        await conn.start()
        await wait_for(lambda: len(delays.picked) >= 3, server, what="retries")
        server.reachable = True
        await wait_for(lambda: conn.is_connected, server, what="first connection")
        picked_before_drop = len(delays.picked)

        server.drop_connections()  # MA restarts after a long, healthy session

        await wait_for(lambda: len(delays.picked) > picked_before_drop, server)
        assert delays.picked[picked_before_drop] == REAL_RECONNECT_DELAYS[0]
        await wait_for(lambda: conn.is_connected, server, what="reconnect")

    async def test_a_connection_that_keeps_dropping_at_once_keeps_backing_off(
        self,
        conn: MusicAssistantConnection,
        server: FakeMusicAssistantServer,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        delays = RecordedDelays(REAL_RECONNECT_DELAYS)
        delays.picked = []
        monkeypatch.setattr(connection_module, "RECONNECT_DELAYS_SECONDS", delays)
        await conn.start()
        for drops in range(1, 4):
            await wait_for(lambda: conn.is_connected, server, what="connection")
            server.drop_connections()
            await wait_for(lambda: len(delays.picked) >= drops, server, what="retry")

        assert delays.picked == list(REAL_RECONNECT_DELAYS[:3])

    @pytest.mark.parametrize("graceful", [True, False], ids=["closed", "error"])
    async def test_lost_connection_is_retried_until_music_assistant_is_back_and_events_flow_again(
        self, conn: MusicAssistantConnection, server: FakeMusicAssistantServer, graceful: bool
    ) -> None:
        await connected(conn, server)
        old_client = server.clients[0]
        states = record_states(conn)
        events: list[Any] = []
        conn.add_event_listener(QID, events.append)

        server.set_reachable(False)
        server.drop_connections(graceful=graceful)
        await wait_for(lambda: not conn.is_connected, server)
        await idle(server, 0.05)
        assert not conn.is_connected  # MA is still down: retrying, no crash
        assert states[0].state == ConnectionState.DISCONNECTED

        server.set_reachable(True)
        await wait_for(lambda: conn.is_connected, server)
        assert new_client(server, old_client)
        assert states[-1].state == ConnectionState.CONNECTED
        events.clear()
        server.start_playing(QID, ["1"])
        await wait_for(lambda: any(e.event == EventType.QUEUE_UPDATED for e in events), server)

    async def test_hung_initial_load_times_out_and_retries(
        self,
        conn: MusicAssistantConnection,
        server: FakeMusicAssistantServer,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        monkeypatch.setattr(connection_module, "LOAD_TIMEOUT_SECONDS", 0.05)
        states = record_states(conn)
        server.gate("players/all")
        await conn.start()

        await wait_for(lambda: len(server.created_clients) >= 3, server, what="retries")
        assert ConnectionState.CONNECTED not in [s.state for s in states]
        assert any(
            s.state == ConnectionState.DISCONNECTED and server.url in s.message for s in states
        )
        assert len(server.clients) <= 1  # hung clients are closed, not leaked

        server.release("players/all")
        await wait_for(lambda: conn.is_connected, server)


class TestAuth:
    async def test_revoked_token_stops_retrying(
        self, conn: MusicAssistantConnection, server: FakeMusicAssistantServer
    ) -> None:
        await connected(conn, server)

        server.revoke_token(server.token)

        await wait_for(lambda: conn.status.state == ConnectionState.AUTH_FAILED, server)
        attempts = len(server.created_clients)
        await idle(server, 0.1)  # ~10 retry delays
        assert len(server.created_clients) == attempts
        assert conn.status.state == ConnectionState.AUTH_FAILED

    async def test_rejected_token_is_tried_once_and_a_new_one_connects(
        self, server: FakeMusicAssistantServer
    ) -> None:
        conn = MusicAssistantConnection(server.url, "bogus", client_factory=server.client_factory)
        try:
            await conn.start()
            await wait_for(lambda: conn.status.state == ConnectionState.AUTH_FAILED, server)
            await idle(server, 0.05)
            assert len(server.created_clients) == 1

            await conn.reconnect(server.url, server.token)

            await wait_for(lambda: conn.is_connected, server)
        finally:
            await conn.stop()

    @pytest.mark.parametrize(
        ("setting", "refused", "fixed"),
        [("setup_required", True, False), ("min_schema_version", 999, 28)],
        ids=["setup_required", "unsupported_version"],
    )
    async def test_refused_connection_is_retried_not_treated_as_a_bad_login(
        self,
        conn: MusicAssistantConnection,
        server: FakeMusicAssistantServer,
        setting: str,
        refused: Any,
        fixed: Any,
    ) -> None:
        states = record_states(conn)
        setattr(server, setting, refused)
        await conn.start()
        await wait_for(lambda: len(server.created_clients) >= 3, server, what="retries")
        assert ConnectionState.DISCONNECTED in [s.state for s in states]
        assert ConnectionState.AUTH_FAILED not in [s.state for s in states]

        setattr(server, setting, fixed)
        await wait_for(lambda: conn.is_connected, server)

    async def test_logs_never_contain_the_token(
        self,
        conn: MusicAssistantConnection,
        server: FakeMusicAssistantServer,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        with caplog.at_level(logging.DEBUG):
            await connected(conn, server)
            server.revoke_token(server.token)
            await wait_for(lambda: conn.status.state == ConnectionState.AUTH_FAILED, server)
        assert caplog.text
        assert server.token not in caplog.text


class TestCall:
    async def test_returns_the_result(
        self, conn: MusicAssistantConnection, server: FakeMusicAssistantServer
    ) -> None:
        await connected(conn, server)
        server.start_playing(QID, ["1", "2"])
        items = await conn.call("queue items", lambda c: c.player_queues.get_queue_items(QID))
        assert [i.media_item.item_id for i in items] == ["1", "2"]

    async def test_not_connected_raises_without_sending(
        self, conn: MusicAssistantConnection, server: FakeMusicAssistantServer
    ) -> None:
        with pytest.raises(NotConnectedError):
            await conn.call("stop", lambda c: c.player_queues.stop(QID))
        assert server.command_log == []

    async def test_music_assistant_error_is_reraised_unchanged_and_keeps_the_connection(
        self, conn: MusicAssistantConnection, server: FakeMusicAssistantServer
    ) -> None:
        await connected(conn, server)
        server.fail_next["player_queues/play_media"] = MediaNotFoundError("Item 9 not found")

        with pytest.raises(MediaNotFoundError, match="Item 9 not found") as err:
            await conn.call(
                "play_media", lambda c: c.player_queues.play_media(QID, "qobuz://track/9")
            )
        assert type(err.value) is MediaNotFoundError
        assert conn.is_connected

    @pytest.mark.parametrize(
        "error",
        [
            InvalidState("Not connected"),
            NotConnected(),
            ConnectionClosed("closed"),
            ConnectionFailed(),
            aiohttp.ClientConnectionError("reset"),
            ConnectionResetError("reset"),
        ],
        ids=lambda e: type(e).__name__,
    )
    async def test_connection_errors_map_to_not_connected(
        self, conn: MusicAssistantConnection, server: FakeMusicAssistantServer, error: Exception
    ) -> None:
        await connected(conn, server)

        async def fail(client: Any) -> None:
            raise error

        with pytest.raises(NotConnectedError):
            await conn.call("stop", fail)

    @pytest.mark.parametrize("failure", [DROP, DROP_HARD])
    async def test_socket_drop_mid_call_maps_to_not_connected(
        self, conn: MusicAssistantConnection, server: FakeMusicAssistantServer, failure: str
    ) -> None:
        await connected(conn, server)
        old_client = server.clients[0]
        server.fail_next["player_queues/stop"] = failure

        with pytest.raises(NotConnectedError):
            await conn.call("stop", lambda c: c.player_queues.stop(QID))

        await wait_for(lambda: conn.is_connected and new_client(server, old_client), server)

    async def test_our_own_cancellation_propagates(
        self, conn: MusicAssistantConnection, server: FakeMusicAssistantServer
    ) -> None:
        await connected(conn, server)
        server.gate("player_queues/stop")
        task = asyncio.create_task(conn.call("stop", lambda c: c.player_queues.stop(QID)))
        await server.settle()
        assert not task.done()

        task.cancel()

        with pytest.raises(asyncio.CancelledError):
            await task
        server.release("player_queues/stop")
        assert conn.is_connected

    async def pause_times_out(
        self, conn: MusicAssistantConnection, server: FakeMusicAssistantServer
    ) -> None:
        server.fail_next["player_queues/pause"] = HANG
        with pytest.raises(NotConnectedError):
            await conn.call("pause", lambda c: c.player_queues.pause(QID), timeout=0.05)

    async def test_two_timeouts_in_a_row_force_a_reconnect(
        self, conn: MusicAssistantConnection, server: FakeMusicAssistantServer
    ) -> None:
        await connected(conn, server)
        old_client = server.clients[0]
        await self.pause_times_out(conn, server)
        await self.pause_times_out(conn, server)

        await wait_for(lambda: conn.is_connected and new_client(server, old_client), server)

    async def test_timeouts_separated_by_an_answer_keep_the_connection(
        self, conn: MusicAssistantConnection, server: FakeMusicAssistantServer
    ) -> None:
        await connected(conn, server)
        client = server.clients[0]

        await self.pause_times_out(conn, server)
        await conn.call("pause", lambda c: c.player_queues.pause(QID))
        await self.pause_times_out(conn, server)

        await idle(server, 0.05)
        assert server.clients == [client]


class TestListeners:
    async def test_event_listeners_per_object_and_global(
        self, conn: MusicAssistantConnection, server: FakeMusicAssistantServer
    ) -> None:
        await connected(conn, server)
        mine: list[Any] = []
        other: list[Any] = []
        everything: list[Any] = []
        conn.add_event_listener(QID, mine.append)
        conn.add_event_listener("up2", other.append)
        conn.add_event_listener(None, everything.append)

        server.start_playing(QID, ["1"])
        server.add_player("up3", "Hall")
        await server.settle()

        assert mine and all(e.object_id == QID for e in mine)
        assert other == []
        assert {e.object_id for e in everything} >= {QID, "up3"}
        assert len(everything) > len(mine)

    async def test_listener_sees_the_updated_cache(
        self, conn: MusicAssistantConnection, server: FakeMusicAssistantServer
    ) -> None:
        await connected(conn, server)
        server.start_playing(QID, ["1"])
        await server.settle()
        seen: list[Any] = []

        def on_event(event: Any) -> None:
            if event.event == EventType.QUEUE_UPDATED:
                queue = conn.queue(QID)
                assert queue is not None
                seen.append((queue.state, event.data["state"]))

        conn.add_event_listener(QID, on_event)
        server.ui_pause(QID)
        await server.settle()

        assert seen and seen[-1] == (PlaybackState.PAUSED, "paused")
        assert all(cached == wire for cached, wire in seen)

    async def test_failing_listener_does_not_break_dispatch(
        self, conn: MusicAssistantConnection, server: FakeMusicAssistantServer
    ) -> None:
        await connected(conn, server)

        def broken(event: Any) -> None:
            raise RuntimeError("listener bug")

        received: list[Any] = []
        conn.add_event_listener(QID, broken)
        conn.add_event_listener(QID, received.append)

        server.start_playing(QID, ["1"])
        await server.settle()
        assert received
        assert conn.is_connected
        received.clear()
        server.ui_pause(QID)
        await server.settle()
        assert received  # the client keeps dispatching

    async def test_removed_event_listener_gets_nothing(
        self, conn: MusicAssistantConnection, server: FakeMusicAssistantServer
    ) -> None:
        await connected(conn, server)
        received: list[Any] = []
        conn.add_event_listener(QID, received.append)
        conn.remove_event_listener(QID, received.append)
        server.start_playing(QID, ["1"])
        await server.settle()
        assert received == []

    async def test_status_listeners_survive_a_failing_one_and_can_be_removed(
        self, conn: MusicAssistantConnection, server: FakeMusicAssistantServer
    ) -> None:
        def broken(status: ConnectionStatus) -> None:
            raise RuntimeError("status listener bug")

        seen: list[ConnectionState] = []

        def listener(status: ConnectionStatus) -> None:
            seen.append(status.state)

        conn.add_status_listener(broken)
        conn.add_status_listener(listener)
        await connected(conn, server)
        assert seen[-1] == ConnectionState.CONNECTED

        conn.remove_status_listener(listener)
        server.drop_connections()
        await wait_for(lambda: not conn.is_connected, server)
        assert seen[-1] == ConnectionState.CONNECTED


class TestLifecycle:
    async def test_stop_is_idempotent_and_stops_reconnecting(
        self, conn: MusicAssistantConnection, server: FakeMusicAssistantServer
    ) -> None:
        await conn.stop()  # before start: nothing to do
        await connected(conn, server)

        await conn.stop()
        await conn.stop()

        assert not conn.is_connected
        assert server.clients == []
        attempts = len(server.created_clients)
        await idle(server, 0.05)
        assert len(server.created_clients) == attempts

    async def test_start_is_idempotent(
        self, conn: MusicAssistantConnection, server: FakeMusicAssistantServer
    ) -> None:
        await conn.start()
        await conn.start()
        await wait_for(lambda: conn.is_connected, server)
        assert len(server.created_clients) == 1

    async def test_start_after_stop_connects_again(
        self, conn: MusicAssistantConnection, server: FakeMusicAssistantServer
    ) -> None:
        await connected(conn, server)
        await conn.stop()
        await connected(conn, server)
        assert len(server.clients) == 1

    async def test_reconnect_switches_address_and_token(
        self, conn: MusicAssistantConnection, server: FakeMusicAssistantServer
    ) -> None:
        await connected(conn, server)
        old_client = server.clients[0]
        server.aliases.add("http://ma.local:8095")
        server.valid_tokens.add("second-token")

        await conn.reconnect("http://ma.local:8095", "second-token")

        await wait_for(lambda: conn.is_connected and new_client(server, old_client), server)
        client = server.clients[0]
        assert client.server_url == "http://ma.local:8095"
        assert client.connection.auth_token == "second-token"


class TestLoginAndCreateToken:
    async def test_returns_a_long_lived_token_that_connects(
        self, login_server: FakeMusicAssistantServer
    ) -> None:
        server = login_server

        token, name = await login_and_create_token(server.url, "admin", "admin-password")

        assert name == "admin"
        assert token in server.valid_tokens
        assert server.calls("auth/token/create") == [{"name": "Qobuz Bridge", "user_id": None}]
        conn = MusicAssistantConnection(server.url, token, client_factory=server.client_factory)
        await connected(conn, server)
        await conn.stop()

    async def test_the_temporary_session_token_is_revoked(
        self, login_server: FakeMusicAssistantServer
    ) -> None:
        server = login_server
        before = set(server.valid_tokens)

        token, _name = await login_and_create_token(server.url, "admin", "admin-password")
        await server.settle()

        assert len(server.calls("auth/logout")) == 1
        assert server.valid_tokens == before | {token}  # only the long-lived one is left

    async def test_never_logs_password_or_token(
        self, login_server: FakeMusicAssistantServer, caplog: pytest.LogCaptureFixture
    ) -> None:
        with caplog.at_level(logging.DEBUG):
            token, _ = await login_and_create_token(login_server.url, "admin", "admin-password")
            with pytest.raises(LoginError) as err:
                await login_and_create_token(login_server.url, "admin", "hunter2-typo")
        assert "admin-password" not in caplog.text
        assert "hunter2-typo" not in caplog.text and "hunter2-typo" not in str(err.value)
        assert token not in caplog.text
        assert "fake-session-token" not in caplog.text
