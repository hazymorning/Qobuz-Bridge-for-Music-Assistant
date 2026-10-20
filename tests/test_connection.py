"""The one connection to Music Assistant: status, the players it lists, reconnecting with
backoff, logins and tokens, calls and how they fail, and event listeners.
"""

import asyncio
import logging
from dataclasses import replace

import aiohttp
import pytest
from music_assistant_client.exceptions import InvalidState
from music_assistant_models.enums import PlayerType
from music_assistant_models.errors import MediaNotFoundError

from qobuz_proxy.backends.music_assistant import LoginError
from qobuz_proxy.backends.music_assistant import connection as connection_module
from qobuz_proxy.backends.music_assistant.connection import (
    ConnectionState,
    MusicAssistantConnection,
    NotConnectedError,
    PlayerInfo,
    login_and_create_token,
)
from tests.fake_music_assistant import DROP, HANG, MIN_SCHEMA_VERSION
from tests.rig import PID, idle, wait_for

# Read at import, before the autouse fixture shrinks them for every test
REAL_RECONNECT_DELAYS = connection_module.RECONNECT_DELAYS_SECONDS


@pytest.fixture
async def connection(server):
    """The add-on's connection to the fake Music Assistant, not started yet."""
    connection = MusicAssistantConnection(
        server.url, server.token, client_factory=server.client_factory
    )
    yield connection
    await connection.stop()


def _record_backoff(monkeypatch) -> list[float]:
    """Each real reconnect delay the connection picks, in order; it then waits only 1 ms."""
    picked = []

    class RecordedDelays(tuple):
        def __getitem__(self, index):
            picked.append(super().__getitem__(index))
            return 0.001

    delays = RecordedDelays(REAL_RECONNECT_DELAYS)
    monkeypatch.setattr(connection_module, "RECONNECT_DELAYS_SECONDS", delays)
    return picked


async def _connect(connection, server) -> None:
    await connection.start()
    await wait_for(lambda: connection.is_connected, "connected to Music Assistant", server=server)


def _stop(client):
    return client.player_queues.stop(PID)


def _pause(client):
    return client.player_queues.pause(PID)


def _raising(error):
    async def command(client):
        raise error

    return command


def _broken_listener(event):
    raise RuntimeError("listener bug")


async def _pause_times_out(connection, server) -> None:
    server.fail_next["player_queues/pause"] = HANG
    with pytest.raises(NotConnectedError):
        await connection.call("pause", _pause, timeout=0.05)


async def test_status_shows_the_version_and_whether_qobuz_is_a_working_provider(connection, server):
    await _connect(connection, server)
    assert connection.status.state == ConnectionState.CONNECTED
    assert connection.status.server_version == server.server_version
    assert connection.status.has_qobuz_provider

    server.restart()
    # This time its Qobuz provider failed to load, while another streaming provider works
    qobuz = server.add_qobuz_provider(available=False)
    server.providers.append(replace(qobuz, domain="spotify", instance_id="spotify", available=True))
    server.come_back()
    await wait_for(
        lambda: len(server.created_clients) == 2 and connection.is_connected,
        "reconnected after the restart",
        server=server,
    )
    assert not connection.status.has_qobuz_provider


async def test_lists_only_enabled_players_groups_and_stereo_pairs(connection, server):
    server.add_player("group", "Downstairs", type=PlayerType.GROUP, group_members=[PID])
    server.add_player("pair", "Living Room", type=PlayerType.STEREO_PAIR, available=False)
    server.add_player("hidden", "Bathroom", hide_in_ui=True)
    await _connect(connection, server)
    # Music Assistant's initial player list leaves these out; they only arrive as events
    server.add_player("protocol", "Kitchen (AirPlay)", type=PlayerType.PROTOCOL)
    server.add_player("disabled", "Garage", enabled=False)
    await wait_for(
        lambda: connection.player("protocol") and connection.player("disabled"),
        "the protocol and disabled players",
        server=server,
    )

    assert set(connection.players()) == {
        PlayerInfo(PID, "Kitchen", "player", available=True, hidden=False),
        PlayerInfo("group", "Downstairs", "group", available=True, hidden=False),
        PlayerInfo("pair", "Living Room", "stereo_pair", available=False, hidden=False),
        PlayerInfo("hidden", "Bathroom", "player", available=True, hidden=True),
    }


async def test_events_reach_the_listeners_of_their_player_even_when_another_listener_fails(
    connection, server
):
    await _connect(connection, server)
    everything, kitchen, bedroom = [], [], []
    # Registered first, so every other listener gets each event after it has failed
    connection.add_event_listener(None, _broken_listener)
    connection.add_event_listener(None, everything.append)
    connection.add_event_listener(PID, kitchen.append)
    connection.add_event_listener("bedroom", bedroom.append)

    server.start_playing(PID, ["1"])
    await server.settle()

    assert everything and kitchen
    assert {event.object_id for event in kitchen} == {PID}
    assert bedroom == []


async def test_a_call_passes_music_assistant_errors_and_cancellation_through_and_stays_connected(
    connection, server
):
    await _connect(connection, server)
    server.fail_next["player_queues/play_media"] = MediaNotFoundError("Item 9 not found")
    with pytest.raises(MediaNotFoundError, match="Item 9 not found"):
        await connection.call(
            "play", lambda client: client.player_queues.play_media(PID, "qobuz://track/9")
        )

    server.gate("player_queues/stop")
    stopping = asyncio.create_task(connection.call("stop", _stop))
    await server.settle()
    assert server.calls("player_queues/stop")  # sent, waiting for the answer
    stopping.cancel()
    with pytest.raises(asyncio.CancelledError):
        await stopping
    server.release("player_queues/stop")

    assert connection.is_connected and len(server.created_clients) == 1


async def test_a_call_raises_not_connected_when_there_is_no_connection_or_it_is_lost(
    connection, server
):
    with pytest.raises(NotConnectedError):
        await connection.call("stop", _stop)
    assert server.command_log == []

    await _connect(connection, server)
    # What the client raises when its socket is already gone
    for error in (
        InvalidState("Not connected"),
        aiohttp.ClientConnectionError("Connection reset"),
        ConnectionResetError("Connection reset by peer"),
    ):
        with pytest.raises(NotConnectedError):
            await connection.call("stop", _raising(error))
    server.fail_next["player_queues/stop"] = DROP  # the socket closes before the answer
    with pytest.raises(NotConnectedError):
        await connection.call("stop", _stop)


async def test_two_call_timeouts_in_a_row_force_a_reconnect(connection, server):
    await _connect(connection, server)
    await _pause_times_out(connection, server)
    await connection.call("pause", _pause)  # an answer in between: the socket is alive
    await _pause_times_out(connection, server)
    await server.settle()
    assert connection.is_connected and len(server.created_clients) == 1

    await _pause_times_out(connection, server)
    await wait_for(
        lambda: len(server.created_clients) == 2 and connection.is_connected,
        "a new connection",
        server=server,
    )


async def test_unreachable_music_assistant_is_retried_with_growing_backoff_up_to_a_cap(
    connection, server, monkeypatch
):
    backoff = _record_backoff(monkeypatch)
    messages = []
    connection.add_status_listener(lambda status: messages.append(status.message))
    server.reachable = False
    await connection.start()
    await wait_for(lambda: len(backoff) >= 9, "nine retries", server=server)

    assert backoff[:9] == [1, 2, 5, 10, 20, 30, 60, 60, 60]
    assert any(server.url in message for message in messages)  # the panel names the address
    server.reachable = True
    await wait_for(lambda: connection.is_connected, "connected once reachable", server=server)


async def test_only_a_connection_that_lasted_starts_the_backoff_over(
    connection, server, monkeypatch
):
    backoff = _record_backoff(monkeypatch)
    await _connect(connection, server)
    for drops in (1, 2, 3):  # each new connection drops at once
        server.drop_connections()
        await wait_for(
            lambda: len(backoff) == drops and connection.is_connected, "a reconnect", server=server
        )
    assert backoff == [1, 2, 5]

    monkeypatch.setattr(connection_module, "STABLE_CONNECTION_SECONDS", 0.0)  # this one lasted
    server.drop_connections()
    await wait_for(lambda: len(backoff) == 4, "a retry", server=server)
    assert backoff[-1] == 1


async def test_a_hung_initial_load_times_out_and_is_retried(connection, server, monkeypatch):
    monkeypatch.setattr(connection_module, "LOAD_TIMEOUT_SECONDS", 0.05)
    server.gate("players/all")  # Music Assistant never sends its players
    await connection.start()
    await wait_for(lambda: len(server.created_clients) >= 3, "three attempts", server=server)

    assert not connection.is_connected
    assert len(server.clients) <= 1  # the hung connections were closed
    server.release("players/all")
    await wait_for(lambda: connection.is_connected, "connected once it answers", server=server)


async def test_a_refused_connection_is_retried_but_a_revoked_token_stops_the_retries(
    connection, server
):
    server.setup_required = True  # a fresh Music Assistant closes the socket during login
    await connection.start()
    await wait_for(lambda: len(server.created_clients) >= 3, "three attempts", server=server)
    server.setup_required = False
    server.min_schema_version = 999  # then it is too new for this add-on
    attempts = len(server.created_clients)
    await wait_for(
        lambda: len(server.created_clients) >= attempts + 2, "two more attempts", server=server
    )
    server.min_schema_version = MIN_SCHEMA_VERSION
    await wait_for(lambda: connection.is_connected, "connected once it accepts us", server=server)

    server.revoke_token(server.token)
    await wait_for(
        lambda: connection.status.state == ConnectionState.AUTH_FAILED,
        "the token rejected",
        server=server,
    )
    attempts = len(server.created_clients)
    await idle(server, 0.1)  # five retry delays
    assert len(server.created_clients) == attempts


@pytest.mark.usefixtures("fake_login")
async def test_logging_in_at_a_new_address_reconnects_there_with_a_long_lived_token(
    connection, server
):
    await _connect(connection, server)
    new_address = "http://music-assistant.local:8095"
    server.aliases.add(new_address)
    tokens_before = set(server.valid_tokens)

    token, name = await login_and_create_token(new_address, "admin", "admin-password")
    await connection.reconnect(new_address, token)
    await wait_for(lambda: connection.is_connected, "connected at the new address", server=server)

    assert name == "admin"
    assert server.calls("auth/token/create") == [{"name": "MA Qobuz Bridge", "user_id": None}]
    assert server.valid_tokens == tokens_before | {token}  # the login's session token is revoked
    assert [(client.server_url, client.connection.auth_token) for client in server.clients] == [
        (new_address, token)
    ]


@pytest.mark.usefixtures("fake_login")
async def test_the_password_and_tokens_never_reach_the_logs(connection, server, caplog):
    with caplog.at_level(logging.DEBUG):
        # The client's real websocket logs every frame at debug level, the auth token included
        assert not logging.getLogger("music_assistant_client.connection").isEnabledFor(
            logging.DEBUG
        )
        with pytest.raises(LoginError) as wrong_password:
            await login_and_create_token(server.url, "admin", "hunter2-typo")
        token, _ = await login_and_create_token(server.url, "admin", "admin-password")
        await connection.reconnect(server.url, token)
        await wait_for(lambda: connection.is_connected, "connected", server=server)
        await connection.call("stop", _stop)
        server.revoke_token(token)
        await wait_for(
            lambda: connection.status.state == ConnectionState.AUTH_FAILED,
            "the token rejected",
            server=server,
        )

    assert caplog.text
    assert "hunter2-typo" not in str(wrong_password.value)
    # "fake-session-token" is the start of every temporary session token the login gets
    for secret in ("hunter2-typo", "admin-password", "fake-session-token", token):
        assert secret not in caplog.text
