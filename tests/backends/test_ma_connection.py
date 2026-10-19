"""MusicAssistantConnection and the login helpers, against the fake MA server.

The fake runs the real ``music-assistant-client``; only the websocket is swapped.
Reconnect/init timeouts are patched to tiny values, and waits are bounded polls.
"""

import asyncio
import logging
from collections.abc import AsyncIterator
from typing import Any, Callable

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

from qobuz_proxy.backends.music_assistant import connection as connection_module  # noqa: E402
from qobuz_proxy.backends.music_assistant.connection import (  # noqa: E402
    MAConnectionState,
    MAConnectionStatus,
    MALoginError,
    MANotConnectedError,
    MAPlayerInfo,
    MusicAssistantConnection,
    login_and_create_token,
    validate_token,
)
from tests.backends.fake_music_assistant import (  # noqa: E402
    DROP,
    DROP_HARD,
    HANG,
    FakeMAServer,
)

QID = "up1"
UNREACHABLE = (
    "Can't reach Music Assistant at http://127.0.0.1:8095. Is the Music Assistant add-on running?"
)
AUTH_FAILED = (
    "Music Assistant no longer accepts the saved login (it may have expired or been removed). "
    "Connect again in this panel."
)
LOST = "Lost the connection to Music Assistant; reconnecting…"
# Captured before the autouse fixture shortens them
REAL_RECONNECT_DELAYS = MusicAssistantConnection.RECONNECT_DELAYS_SECONDS


@pytest.fixture(autouse=True)
def fast_timing(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(MusicAssistantConnection, "RECONNECT_DELAYS_SECONDS", (0.01,))
    monkeypatch.setattr(MusicAssistantConnection, "CONNECT_TIMEOUT_SECONDS", 1.0)
    monkeypatch.setattr(MusicAssistantConnection, "INIT_TIMEOUT_SECONDS", 1.0)


@pytest.fixture
def server() -> FakeMAServer:
    srv = FakeMAServer()
    srv.add_qobuz_provider()
    srv.add_player(QID, "Kitchen")
    return srv


@pytest.fixture
async def conn(server: FakeMAServer) -> AsyncIterator[MusicAssistantConnection]:
    connection = MusicAssistantConnection(
        server.url, server.token, client_factory=server.client_factory
    )
    yield connection
    await connection.stop()


@pytest.fixture
def ma_login(server: FakeMAServer, monkeypatch: pytest.MonkeyPatch) -> FakeMAServer:
    """Point the login helpers at the fake server."""
    monkeypatch.setattr(connection_module, "login", server.login)
    monkeypatch.setattr(connection_module, "MusicAssistantClient", server.client_class)
    return server


async def wait_for(
    cond: Callable[[], bool], server: FakeMAServer, timeout: float = 2.0, what: str = ""
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


async def idle(server: FakeMAServer, seconds: float) -> None:
    """Let real time pass (for 'nothing else happens' checks)."""
    await asyncio.sleep(seconds)
    await server.settle()


def record_states(connection: MusicAssistantConnection) -> list[MAConnectionStatus]:
    states: list[MAConnectionStatus] = []
    connection.add_state_listener(states.append)
    return states


async def connected(conn: MusicAssistantConnection, server: FakeMAServer) -> None:
    await conn.start()
    await wait_for(lambda: conn.is_connected, server, what="CONNECTED")


# ----------------------------------------------------------------------------- connect


class TestConnect:
    async def test_connects_and_reports_status(
        self, conn: MusicAssistantConnection, server: FakeMAServer
    ) -> None:
        states = record_states(conn)
        await connected(conn, server)

        status = conn.status
        assert status.state == MAConnectionState.CONNECTED
        assert status.server_version == "2.10.7"
        assert status.qobuz_provider is True
        assert status.url == server.url
        assert "2.10.7" in status.message
        assert [s.state for s in states] == [
            MAConnectionState.CONNECTING,
            MAConnectionState.CONNECTED,
        ]
        assert conn.client is server.created_clients[-1]

    async def test_no_qobuz_provider(self) -> None:
        server = FakeMAServer()
        server.add_player(QID, "Kitchen")
        conn = MusicAssistantConnection(
            server.url, server.token, client_factory=server.client_factory
        )
        try:
            await connected(conn, server)
            assert conn.status.qobuz_provider is False
        finally:
            await conn.stop()

    async def test_unavailable_qobuz_provider_counts_as_missing(self) -> None:
        server = FakeMAServer()
        server.add_qobuz_provider(available=False)
        server.add_player(QID, "Kitchen")
        conn = MusicAssistantConnection(
            server.url, server.token, client_factory=server.client_factory
        )
        try:
            await connected(conn, server)
            assert conn.status.qobuz_provider is False
        finally:
            await conn.stop()

    async def test_lists_players_groups_and_stereo_pairs_only(
        self, conn: MusicAssistantConnection, server: FakeMAServer
    ) -> None:
        server.add_player("grp", "Downstairs", type=PlayerType.GROUP, group_members=[QID])
        server.add_player("pair", "Living Room", type=PlayerType.STEREO_PAIR)
        server.add_player("shy", "Bathroom", hide_in_ui=True)
        await connected(conn, server)
        # Added while connected: reaches the client cache through PLAYER_ADDED
        server.add_player("proto", "Kitchen (AirPlay)", type=PlayerType.PROTOCOL)
        server.add_player("off", "Garage", enabled=False)
        await wait_for(lambda: conn.get_player("off") is not None, server)
        assert conn.get_player("proto") is not None

        players = {p.player_id: p for p in conn.list_players()}

        assert set(players) == {QID, "grp", "pair", "shy", "off"}
        assert players[QID] == MAPlayerInfo(
            player_id=QID,
            name="Kitchen",
            player_type="player",
            available=True,
            enabled=True,
            hidden=False,
            provider="universal_player",
        )
        assert players["grp"].player_type == "group"
        assert players["pair"].player_type == "stereo_pair"
        assert players["shy"].hidden is True
        assert players["off"].enabled is False

    async def test_state_access_reads_the_cache(
        self, conn: MusicAssistantConnection, server: FakeMAServer
    ) -> None:
        assert conn.list_players() == []
        assert conn.get_player(QID) is None and conn.get_queue(QID) is None
        assert conn.client is None

        await connected(conn, server)
        commands_before = len(server.command_log)
        queue = conn.get_queue(QID)
        assert queue is not None and queue.queue_id == QID
        player = conn.get_player(QID)
        assert player is not None and player.name == "Kitchen"
        assert conn.get_queue("nope") is None and conn.get_player("nope") is None
        assert len(server.command_log) == commands_before  # never calls MA

    async def test_not_configured_without_token(self, server: FakeMAServer) -> None:
        conn = MusicAssistantConnection(server.url, "", client_factory=server.client_factory)
        states = record_states(conn)
        await conn.start()
        await server.settle()

        assert conn.status.state == MAConnectionState.NOT_CONFIGURED
        assert conn.status.message == "Not connected to Music Assistant yet."
        assert all(st.state == MAConnectionState.NOT_CONFIGURED for st in states)
        assert server.created_clients == []
        assert not conn.is_connected
        await conn.stop()

    async def test_not_configured_without_address(self) -> None:
        conn = MusicAssistantConnection()
        assert conn.status.state == MAConnectionState.NOT_CONFIGURED
        await conn.start()
        assert conn.status.state == MAConnectionState.NOT_CONFIGURED
        await conn.stop()

    async def test_url_trailing_slash_is_dropped(self, server: FakeMAServer) -> None:
        conn = MusicAssistantConnection(
            server.url + "/", server.token, client_factory=server.client_factory
        )
        assert conn.url == server.url
        await connected(conn, server)
        assert conn.status.url == server.url
        await conn.stop()


# --------------------------------------------------------------------------- reconnect


class TestReconnect:
    async def test_unreachable_at_start_retries_until_reachable(
        self, conn: MusicAssistantConnection, server: FakeMAServer, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(conn, "RECONNECT_DELAYS_SECONDS", (0.01, 0.02, 0.03))
        server.reachable = False
        await conn.start()

        await wait_for(lambda: len(server.created_clients) >= 3, server, what="retries")
        assert conn.status.state == MAConnectionState.DISCONNECTED
        assert conn.status.message == UNREACHABLE
        assert not conn.is_connected

        server.reachable = True
        await wait_for(lambda: conn.is_connected, server, what="CONNECTED")
        assert conn.status.message.startswith("Connected to Music Assistant")

    def test_backoff_grows_then_stays_at_the_longest_delay(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        conn = MusicAssistantConnection("http://ma:8095", "t")
        monkeypatch.setattr(conn, "RECONNECT_DELAYS_SECONDS", REAL_RECONNECT_DELAYS)
        delays = [conn._backoff_delay(i) for i in range(10)]
        assert delays == [1.0, 2.0, 5.0, 10.0, 20.0, 30.0, 60.0, 60.0, 60.0, 60.0]

    async def test_backoff_restarts_after_a_successful_connection(
        self, conn: MusicAssistantConnection, server: FakeMAServer, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(conn, "RECONNECT_DELAYS_SECONDS", (0.01, 30.0))
        server.reachable = False
        await conn.start()
        await wait_for(lambda: len(server.created_clients) >= 1, server)
        server.reachable = True
        await wait_for(lambda: conn.is_connected, server, what="first connection")

        server.drop_connections()  # MA restarts after a long, healthy session

        await wait_for(lambda: not conn.is_connected, server)
        # The first retry must use the shortest delay again, not the 30 s one
        await wait_for(lambda: conn.is_connected, server, timeout=1.0, what="quick reconnect")

    async def test_ma_restart_reconnects_with_a_new_client(
        self, conn: MusicAssistantConnection, server: FakeMAServer
    ) -> None:
        await connected(conn, server)
        old_client = conn.client
        states = record_states(conn)
        events: list[Any] = []
        conn.add_event_listener(QID, events.append)

        server.restart()
        await wait_for(lambda: not conn.is_connected, server)
        assert states[0].state == MAConnectionState.DISCONNECTED
        assert states[0].message == LOST
        await idle(server, 0.05)
        assert not conn.is_connected  # MA is still down: retrying, no crash
        assert conn.status.message == UNREACHABLE

        server.come_back()
        await wait_for(lambda: conn.is_connected, server)

        assert conn.client is not None and conn.client is not old_client
        assert states[-1].state == MAConnectionState.CONNECTED
        # Events flow again through the new client
        events.clear()
        server.start_playing(QID, ["1"])
        await wait_for(lambda: any(e.event == EventType.QUEUE_UPDATED for e in events), server)

    @pytest.mark.parametrize("graceful", [True, False])
    async def test_dropped_socket_reconnects(
        self, conn: MusicAssistantConnection, server: FakeMAServer, graceful: bool
    ) -> None:
        await connected(conn, server)
        old_client = conn.client
        states = record_states(conn)

        server.drop_connections(graceful=graceful)

        await wait_for(
            lambda: conn.is_connected and conn.client is not old_client, server, what="reconnect"
        )
        assert [s.state for s in states][:1] == [MAConnectionState.DISCONNECTED]
        assert states[0].message == LOST
        assert server.clients == [conn.client]


# -------------------------------------------------------------------------------- auth


class TestAuthAndVersion:
    async def test_revoked_token_stops_retrying(
        self, conn: MusicAssistantConnection, server: FakeMAServer
    ) -> None:
        await connected(conn, server)

        server.revoke_token(server.token)

        await wait_for(lambda: conn.status.state == MAConnectionState.AUTH_FAILED, server)
        assert conn.status.message == AUTH_FAILED
        attempts = len(server.created_clients)
        await idle(server, 0.1)  # ~10 retry delays
        assert len(server.created_clients) == attempts
        assert conn.status.state == MAConnectionState.AUTH_FAILED

    async def test_bogus_token_fails_once(self, server: FakeMAServer) -> None:
        conn = MusicAssistantConnection(server.url, "bogus", client_factory=server.client_factory)
        await conn.start()
        await wait_for(lambda: conn.status.state == MAConnectionState.AUTH_FAILED, server)
        await idle(server, 0.05)
        assert len(server.created_clients) == 1
        assert conn.status.message == AUTH_FAILED
        await conn.stop()

    async def test_reconfigure_after_auth_failure_connects(self, server: FakeMAServer) -> None:
        conn = MusicAssistantConnection(server.url, "bogus", client_factory=server.client_factory)
        await conn.start()
        await wait_for(lambda: conn.status.state == MAConnectionState.AUTH_FAILED, server)

        await conn.reconfigure(server.url, server.token)

        await wait_for(lambda: conn.is_connected, server)
        await conn.stop()

    async def test_setup_required_is_not_an_auth_failure(
        self, conn: MusicAssistantConnection, server: FakeMAServer
    ) -> None:
        server.setup_required = True
        await conn.start()
        await wait_for(lambda: len(server.created_clients) >= 2, server, what="retries")
        assert conn.status.state == MAConnectionState.DISCONNECTED
        assert "refused the connection" in conn.status.message

        server.setup_required = False
        await wait_for(lambda: conn.is_connected, server)

    async def test_unsupported_server_version(
        self, conn: MusicAssistantConnection, server: FakeMAServer
    ) -> None:
        server.min_schema_version = 999
        await conn.start()
        await wait_for(lambda: len(server.created_clients) >= 2, server, what="retries")
        assert conn.status.state == MAConnectionState.DISCONNECTED
        assert conn.status.message.startswith("This Music Assistant version is not supported (")

        server.min_schema_version = 28
        await wait_for(lambda: conn.is_connected, server)

    async def test_hung_initial_fetch_times_out_and_retries(
        self, conn: MusicAssistantConnection, server: FakeMAServer, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(conn, "INIT_TIMEOUT_SECONDS", 0.05)
        states = record_states(conn)
        server.hang_init = True
        await conn.start()

        await wait_for(lambda: len(server.created_clients) >= 3, server, what="retries")
        assert MAConnectionState.CONNECTED not in [s.state for s in states]
        assert any(
            st.state == MAConnectionState.DISCONNECTED and st.message == UNREACHABLE
            for st in states
        )
        assert len(server.clients) <= 1  # hung clients are closed, not leaked

        server.hang_init = False
        await wait_for(lambda: conn.is_connected, server)


# -------------------------------------------------------------------------------- call()


class TestCall:
    async def test_call_logs_and_returns_the_result(
        self, conn: MusicAssistantConnection, server: FakeMAServer, caplog: pytest.LogCaptureFixture
    ) -> None:
        await connected(conn, server)
        server.start_playing(QID, ["1", "2"])
        with caplog.at_level(logging.INFO, logger=connection_module.__name__):
            items = await conn.call(
                "player_queues/items queue=up1", lambda c: c.player_queues.get_queue_items(QID)
            )
        assert [i.media_item.item_id for i in items] == ["1", "2"]
        assert "MA → player_queues/items queue=up1" in caplog.text

    async def test_not_connected_raises_without_sending(
        self, conn: MusicAssistantConnection, server: FakeMAServer
    ) -> None:
        with pytest.raises(MANotConnectedError):
            await conn.call("player_queues/stop", lambda c: c.player_queues.stop(QID))
        assert server.command_log == []

    async def test_ma_error_is_reraised_unchanged(
        self, conn: MusicAssistantConnection, server: FakeMAServer, caplog: pytest.LogCaptureFixture
    ) -> None:
        await connected(conn, server)
        server.fail_next["player_queues/play_media"] = MediaNotFoundError("Item 9 not found")

        with caplog.at_level(logging.WARNING, logger=connection_module.__name__):
            with pytest.raises(MediaNotFoundError, match="Item 9 not found") as err:
                await conn.call(
                    "player_queues/play_media",
                    lambda c: c.player_queues.play_media(QID, "qobuz://track/9"),
                )
        assert type(err.value) is MediaNotFoundError
        assert "MA ✗ player_queues/play_media" in caplog.text
        assert conn.is_connected

    @pytest.mark.parametrize(
        "error",
        [
            InvalidState("Not connected"),
            NotConnected(),
            ConnectionClosed("closed"),
            ConnectionFailed(),
        ],
        ids=["InvalidState", "NotConnected", "ConnectionClosed", "ConnectionFailed"],
    )
    async def test_client_connection_errors_map_to_not_connected(
        self, conn: MusicAssistantConnection, server: FakeMAServer, error: Exception
    ) -> None:
        await connected(conn, server)

        async def fail(client: Any) -> None:
            raise error

        with pytest.raises(MANotConnectedError):
            await conn.call("players/cmd/stop", fail)

    @pytest.mark.parametrize("failure", [DROP, DROP_HARD])
    async def test_socket_drop_mid_call_maps_to_not_connected(
        self, conn: MusicAssistantConnection, server: FakeMAServer, failure: str
    ) -> None:
        await connected(conn, server)
        old_client = conn.client
        server.fail_next["player_queues/stop"] = failure

        with pytest.raises(MANotConnectedError):
            await conn.call("player_queues/stop", lambda c: c.player_queues.stop(QID))

        await wait_for(lambda: conn.is_connected and conn.client is not old_client, server)

    async def test_our_own_cancellation_propagates(
        self, conn: MusicAssistantConnection, server: FakeMAServer
    ) -> None:
        await connected(conn, server)
        server.gate("player_queues/stop")
        task = asyncio.create_task(
            conn.call("player_queues/stop", lambda c: c.player_queues.stop(QID))
        )
        await server.settle()
        assert not task.done()

        task.cancel()

        with pytest.raises(asyncio.CancelledError):
            await task
        server.release("player_queues/stop")
        assert conn.is_connected

    async def test_timeout_maps_to_not_connected(
        self, conn: MusicAssistantConnection, server: FakeMAServer
    ) -> None:
        await connected(conn, server)
        client = conn.client
        server.fail_next["player_queues/pause"] = HANG

        with pytest.raises(MANotConnectedError):
            await conn.call(
                "player_queues/pause", lambda c: c.player_queues.pause(QID), timeout=0.05
            )

        await idle(server, 0.05)
        assert conn.is_connected and conn.client is client  # one timeout is not a dead socket

    async def test_two_timeouts_in_a_row_force_a_reconnect(
        self, conn: MusicAssistantConnection, server: FakeMAServer
    ) -> None:
        await connected(conn, server)
        client = conn.client
        for _ in range(2):
            server.fail_next["player_queues/pause"] = HANG
            with pytest.raises(MANotConnectedError):
                await conn.call(
                    "player_queues/pause", lambda c: c.player_queues.pause(QID), timeout=0.05
                )

        await wait_for(lambda: conn.is_connected and conn.client is not client, server)

    async def test_an_answer_resets_the_timeout_count(
        self, conn: MusicAssistantConnection, server: FakeMAServer
    ) -> None:
        await connected(conn, server)
        client = conn.client

        server.fail_next["player_queues/pause"] = HANG
        with pytest.raises(MANotConnectedError):
            await conn.call(
                "player_queues/pause", lambda c: c.player_queues.pause(QID), timeout=0.05
            )
        await conn.call("player_queues/pause", lambda c: c.player_queues.pause(QID))
        server.fail_next["player_queues/pause"] = HANG
        with pytest.raises(MANotConnectedError):
            await conn.call(
                "player_queues/pause", lambda c: c.player_queues.pause(QID), timeout=0.05
            )

        await idle(server, 0.05)
        assert conn.client is client


# --------------------------------------------------------------------------- listeners


class TestListeners:
    async def test_event_listeners_per_object_and_global(
        self, conn: MusicAssistantConnection, server: FakeMAServer
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
        self, conn: MusicAssistantConnection, server: FakeMAServer
    ) -> None:
        await connected(conn, server)
        server.start_playing(QID, ["1"])
        await server.settle()
        seen: list[Any] = []

        def on_event(event: Any) -> None:
            if event.event == EventType.QUEUE_UPDATED:
                queue = conn.get_queue(QID)
                assert queue is not None
                seen.append((queue.state, event.data["state"]))

        conn.add_event_listener(QID, on_event)
        server.ui_pause(QID)
        await server.settle()

        assert seen and seen[-1] == (PlaybackState.PAUSED, "paused")
        assert all(cached == wire for cached, wire in seen)

    async def test_failing_listener_does_not_break_dispatch(
        self, conn: MusicAssistantConnection, server: FakeMAServer, caplog: pytest.LogCaptureFixture
    ) -> None:
        await connected(conn, server)

        def broken(event: Any) -> None:
            raise RuntimeError("listener bug")

        received: list[Any] = []
        conn.add_event_listener(QID, broken)
        conn.add_event_listener(QID, received.append)

        with caplog.at_level(logging.ERROR, logger=connection_module.__name__):
            server.start_playing(QID, ["1"])
            await server.settle()

        assert received
        assert "listener bug" in caplog.text
        assert conn.is_connected
        received.clear()
        server.ui_pause(QID)
        await server.settle()
        assert received  # the client keeps dispatching

    async def test_unsubscribe(self, conn: MusicAssistantConnection, server: FakeMAServer) -> None:
        await connected(conn, server)
        received: list[Any] = []
        remove = conn.add_event_listener(QID, received.append)
        remove()
        remove()  # twice is fine
        server.start_playing(QID, ["1"])
        await server.settle()
        assert received == []

    async def test_state_listeners_contain_errors_and_unsubscribe(
        self, conn: MusicAssistantConnection, server: FakeMAServer
    ) -> None:
        def broken(status: MAConnectionStatus) -> None:
            raise RuntimeError("state listener bug")

        seen: list[MAConnectionState] = []
        conn.add_state_listener(broken)
        remove = conn.add_state_listener(lambda s: seen.append(s.state))
        await connected(conn, server)
        assert seen[-1] == MAConnectionState.CONNECTED

        remove()
        server.drop_connections()
        await wait_for(lambda: not conn.is_connected, server)
        assert seen[-1] == MAConnectionState.CONNECTED


# --------------------------------------------------------------------------- lifecycle


class TestLifecycle:
    async def test_stop_is_idempotent_and_stops_reconnecting(
        self, conn: MusicAssistantConnection, server: FakeMAServer
    ) -> None:
        await connected(conn, server)

        await conn.stop()
        await conn.stop()

        assert not conn.is_connected and conn.client is None
        assert server.clients == []
        attempts = len(server.created_clients)
        await idle(server, 0.05)
        assert len(server.created_clients) == attempts

    async def test_stop_before_start(self, server: FakeMAServer) -> None:
        conn = MusicAssistantConnection(
            server.url, server.token, client_factory=server.client_factory
        )
        await conn.stop()
        await conn.stop()

    async def test_start_is_idempotent(
        self, conn: MusicAssistantConnection, server: FakeMAServer
    ) -> None:
        await conn.start()
        await conn.start()
        await wait_for(lambda: conn.is_connected, server)
        assert len(server.created_clients) == 1

    async def test_start_after_stop_connects_again(
        self, conn: MusicAssistantConnection, server: FakeMAServer
    ) -> None:
        await connected(conn, server)
        await conn.stop()
        await connected(conn, server)
        assert len(server.clients) == 1

    async def test_reconfigure_switches_address_and_token(
        self, conn: MusicAssistantConnection, server: FakeMAServer
    ) -> None:
        await connected(conn, server)
        old_client = conn.client
        server.aliases.add("http://ma.local:8095")
        server.valid_tokens.add("second-token")

        await conn.reconfigure("http://ma.local:8095/", "second-token")

        await wait_for(lambda: conn.is_connected and conn.client is not old_client, server)
        assert conn.url == "http://ma.local:8095"
        assert conn.status.url == "http://ma.local:8095"
        assert server.clients == [conn.client]

    async def test_logs_never_contain_the_token(
        self, conn: MusicAssistantConnection, server: FakeMAServer, caplog: pytest.LogCaptureFixture
    ) -> None:
        with caplog.at_level(logging.DEBUG):
            await connected(conn, server)
            server.revoke_token(server.token)
            await wait_for(lambda: conn.status.state == MAConnectionState.AUTH_FAILED, server)
        assert caplog.text
        assert server.token not in caplog.text


# ------------------------------------------------------------------------ login helpers


class TestLoginAndCreateToken:
    async def test_success_returns_a_long_lived_token(self, ma_login: FakeMAServer) -> None:
        server = ma_login

        token, name = await login_and_create_token(server.url, "admin", "admin-password")

        assert name == "admin"
        assert token in server.valid_tokens
        assert server.calls("auth/token/create") == [{"name": "Qobuz Bridge", "user_id": None}]
        conn = MusicAssistantConnection(server.url, token, client_factory=server.client_factory)
        await connected(conn, server)
        await conn.stop()

    async def test_the_temporary_session_token_is_revoked(self, ma_login: FakeMAServer) -> None:
        server = ma_login
        before = set(server.valid_tokens)

        token, _name = await login_and_create_token(server.url, "admin", "admin-password")
        await server.settle()

        assert len(server.calls("auth/logout")) == 1
        assert server.valid_tokens == before | {token}  # only the long-lived one is left

    async def test_custom_token_name_and_trailing_slash(self, ma_login: FakeMAServer) -> None:
        server = ma_login
        await login_and_create_token(
            server.url + "/", "admin", "admin-password", token_name="Kitchen bridge"
        )
        assert server.calls("auth/token/create")[-1]["name"] == "Kitchen bridge"

    async def test_wrong_password(self, ma_login: FakeMAServer) -> None:
        server = ma_login
        before = set(server.valid_tokens)
        with pytest.raises(MALoginError) as err:
            await login_and_create_token(server.url, "admin", "wrong")
        assert "did not accept that username and password" in str(err.value)
        assert "long-lived token" in str(err.value)
        assert server.valid_tokens == before

    async def test_setup_required(self, ma_login: FakeMAServer) -> None:
        ma_login.setup_required = True
        with pytest.raises(MALoginError) as err:
            await login_and_create_token(ma_login.url, "admin", "admin-password")
        assert "not set up yet" in str(err.value)

    async def test_unreachable(self, ma_login: FakeMAServer) -> None:
        ma_login.reachable = False
        with pytest.raises(MALoginError) as err:
            await login_and_create_token(ma_login.url, "admin", "admin-password")
        assert str(err.value).startswith("Can't reach Music Assistant at http://127.0.0.1:8095")

    async def test_guest_user(self, ma_login: FakeMAServer) -> None:
        ma_login.users["guest"] = "guest-password"
        ma_login.guest_users.add("guest")
        with pytest.raises(MALoginError) as err:
            await login_and_create_token(ma_login.url, "guest", "guest-password")
        assert "guest" in str(err.value)
        assert ma_login.calls("auth/token/create")  # MA refused it

    async def test_never_logs_password_or_token(
        self, ma_login: FakeMAServer, caplog: pytest.LogCaptureFixture
    ) -> None:
        with caplog.at_level(logging.DEBUG):
            token, _ = await login_and_create_token(ma_login.url, "admin", "admin-password")
            with pytest.raises(MALoginError) as err:
                await login_and_create_token(ma_login.url, "admin", "hunter2-typo")
        assert "admin-password" not in caplog.text
        assert "hunter2-typo" not in caplog.text and "hunter2-typo" not in str(err.value)
        assert token not in caplog.text
        assert "fake-session-token" not in caplog.text


class TestValidateToken:
    async def test_good_token_returns_the_version_and_user(self, ma_login: FakeMAServer) -> None:
        version, user = await validate_token(ma_login.url, ma_login.token)
        assert (version, user) == ("2.10.7", "admin")
        assert ma_login.clients == []  # disconnected again

    async def test_bad_token(self, ma_login: FakeMAServer) -> None:
        with pytest.raises(MALoginError, match="did not accept that token"):
            await validate_token(ma_login.url, "bogus")

    async def test_unreachable(self, ma_login: FakeMAServer) -> None:
        ma_login.reachable = False
        with pytest.raises(MALoginError, match="Can't reach Music Assistant"):
            await validate_token(ma_login.url, ma_login.token)

    async def test_unsupported_version(self, ma_login: FakeMAServer) -> None:
        ma_login.min_schema_version = 999
        with pytest.raises(MALoginError, match="not supported"):
            await validate_token(ma_login.url, ma_login.token)

    async def test_token_never_in_message_or_logs(
        self, ma_login: FakeMAServer, caplog: pytest.LogCaptureFixture
    ) -> None:
        with caplog.at_level(logging.DEBUG):
            with pytest.raises(MALoginError) as err:
                await validate_token(ma_login.url, "bogus-secret-xyz")
        assert "bogus-secret-xyz" not in str(err.value)
        assert "bogus-secret-xyz" not in caplog.text
