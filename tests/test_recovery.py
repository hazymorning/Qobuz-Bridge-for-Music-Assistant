"""Recovering on its own: Music Assistant unreachable at start, going away or restarting
mid-play, a revoked Music Assistant token, and an add-on restart while the music plays.
"""

import pytest
from music_assistant_models.enums import PlaybackState as MusicAssistantState

from qobuz_proxy.backends.music_assistant import settings
from qobuz_proxy.backends.music_assistant.connection import ConnectionState
from tests.fake_qobuz import APP_PAUSED, APP_PLAYING, APP_STOPPED, album_state, uri
from tests.rig import PID, Rig, assert_no_backend_errors


@pytest.fixture
async def start_speaker(server, monkeypatch):
    """Start the Kitchen speaker (from saved sessions, if given) and pick it in the Qobuz app."""
    started = []

    async def start(sessions=None, *, wait_for_music_assistant=True):
        rig = Rig(server, monkeypatch, sessions=sessions)
        started.append(rig)
        await rig.start(wait_for_music_assistant=wait_for_music_assistant)
        await rig.select()
        return rig

    yield start
    for rig in started:
        await rig.close()


@pytest.fixture
def restart_add_on(rig, start_speaker, tmp_path):
    """Stop the add-on (Music Assistant plays on) and start it again from its settings file."""

    async def restart():
        await rig.close()
        path = tmp_path / "music_assistant.json"
        settings.save(path, settings.Settings(sessions=rig.sessions))
        return await start_speaker(settings.load(path).sessions)

    return restart


async def test_while_music_assistant_is_unreachable_at_start_play_fails_cleanly_until_it_is_back(
    server, start_speaker, caplog
):
    server.set_reachable(False)
    rig = await start_speaker(wait_for_music_assistant=False)  # in the Qobuz app all the same

    await rig.deliver(album_state(0))
    await rig.wait_for(lambda: rig.app() == (APP_STOPPED, 1), "app shows stopped")
    assert rig.cloud.actions == []  # unlike an unplayable track, nothing is skipped

    server.set_reachable(True)
    await rig.wait_for(lambda: rig.conn.is_connected, "Music Assistant connected")
    await rig.play_album(0)
    assert rig.app() == (APP_PLAYING, 1)
    assert rig.plays()[0] == ("replace", [uri("101")])
    assert_no_backend_errors(caplog)


async def test_a_connection_drop_shorter_than_the_grace_changes_nothing(server, rig):
    await rig.play_album(0)
    await rig.wait_armed("102")
    server.advance_time(PID, 30)
    server.clear_logs()
    clients, reports = len(server.created_clients), len(rig.cloud.reports)

    server.drop_connections()
    await rig.wait_for(
        lambda: len(server.created_clients) > clients and rig.conn.is_connected, "reconnected"
    )
    await rig.idle(0.4)  # longer than the grace

    assert rig.shown_states(since=reports) <= {APP_PLAYING}
    assert rig.app() == (APP_PLAYING, 1)
    assert server.writes == []
    assert rig.queue_track() == "101" and rig.queue_state() == MusicAssistantState.PLAYING


async def test_an_outage_longer_than_the_grace_shows_stopped_and_play_works_once_it_is_over(
    server, rig, caplog
):
    await rig.play_album(0)
    await rig.wait_armed("102")

    server.set_reachable(False, drop=True)
    await rig.wait_for(lambda: rig.app() == (APP_STOPPED, 1), "app shows stopped")
    assert not rig.conn.is_connected

    server.set_reachable(True)
    await rig.wait_for(lambda: rig.conn.is_connected, "reconnected")
    await rig.play_album(0)
    assert rig.app() == (APP_PLAYING, 1)
    assert_no_backend_errors(caplog)


async def test_paused_music_stays_paused_through_an_outage(server, rig):
    await rig.play_album(0)
    server.advance_time(PID, 30)
    await rig.deliver(album_state(0, APP_PAUSED, pos=30_000))
    await rig.wait_for(lambda: rig.queue_state() == MusicAssistantState.PAUSED, "paused")

    server.set_reachable(False, drop=True)
    await rig.idle(0.4)  # longer than the grace
    assert rig.app() == (APP_PAUSED, 1)

    server.set_reachable(True)
    await rig.wait_for(lambda: rig.conn.is_connected, "reconnected")
    server.clear_logs()
    await rig.deliver(album_state(0, APP_PLAYING, pos=30_000))
    await rig.wait_for(lambda: rig.app() == (APP_PLAYING, 1), "app shows playing")
    assert rig.writes() == ["player_queues/play"]  # resumed, not started again
    assert server.queue(PID).corrected_elapsed_time == pytest.approx(30, abs=1)


async def test_music_assistant_restart_shows_stopped_where_the_music_stopped_and_play_resumes_there(
    server, rig, caplog
):
    await rig.play_album(0)
    await rig.wait_armed("102")
    server.advance_time(PID, 30)

    server.restart()  # the music stops
    await rig.wait_for(lambda: not rig.conn.is_connected, "connection lost")
    server.remove_player(PID)  # Music Assistant is back before Kitchen registers again
    server.come_back()
    await rig.wait_for(lambda: rig.app()[0] == APP_STOPPED, "app shows stopped")
    server.add_player(PID, "Kitchen")
    await rig.idle(0.2)
    assert rig.app() == (APP_STOPPED, 1)  # Kitchen coming back starts nothing by itself
    assert rig.app_position() == pytest.approx(30_000, abs=2_000)

    await rig.deliver(album_state(0, APP_PLAYING, pos=30_000))
    await rig.wait_for(
        lambda: rig.queue_state() == MusicAssistantState.PLAYING and rig.app() == (APP_PLAYING, 1),
        "playing again",
    )
    assert rig.queue_track() == "101"
    assert server.queue(PID).corrected_elapsed_time == pytest.approx(30, abs=2)
    assert_no_backend_errors(caplog)


async def test_a_revoked_token_stops_the_reconnects_and_the_app_shows_stopped(server, rig):
    await rig.play_album(0)
    clients = len(server.created_clients)

    server.revoke_token(server.token)
    await rig.wait_for(lambda: rig.app() == (APP_STOPPED, 1), "app shows stopped")

    # The grace lasts many reconnect delays, yet it tried only once
    assert len(server.created_clients) == clients + 1
    assert rig.conn.status.state == ConnectionState.AUTH_FAILED  # the panel asks to connect again


async def test_after_an_add_on_restart_the_speaker_ignores_music_assistant_until_the_app_plays(
    server, rig, restart_add_on
):
    await rig.play_album(0)
    server.advance_time(PID, 30)
    server.clear_logs()

    restarted = await restart_add_on()
    server.switch_source(PID, "spotify")  # would make a speaker in use leave the Qobuz session
    await restarted.idle(0.2)

    assert restarted.shown_states() <= {APP_STOPPED}
    assert not restarted.has_left_qobuz_session()
    assert server.writes == []


async def test_after_an_add_on_restart_play_in_the_app_continues_what_music_assistant_plays(
    fast_heartbeat, server, rig, restart_add_on
):
    await rig.play_album(0)
    await rig.wait_armed("102")
    server.advance_time(PID, 60)
    items = server.item_ids(PID)
    server.clear_logs()

    restarted = await restart_add_on()
    # The Qobuz cloud replays its old snapshot (paused at 0:04), then the user presses play
    await restarted.deliver(album_state(0, APP_PAUSED, pos=4_000))
    await restarted.deliver(album_state(0, APP_PLAYING, pos=4_000))
    await restarted.wait_for(
        lambda: restarted.app() == (APP_PLAYING, 1) and restarted.app_position() >= 59_000,
        "the app shows where Music Assistant is",
    )
    await restarted.wait_armed("102")

    assert server.writes == []  # no stop, restart, seek back or second copy of the next track
    assert server.item_ids(PID) == items
    assert server.queue(PID).corrected_elapsed_time >= 60


async def test_after_an_add_on_restart_play_resumes_the_track_paused_in_music_assistant(
    server, rig, restart_add_on
):
    await rig.play_album(0)
    server.advance_time(PID, 30)
    server.ui_pause(PID)
    server.clear_logs()

    restarted = await restart_add_on()
    await restarted.deliver(album_state(0, APP_PLAYING, pos=30_000))
    await restarted.wait_for(lambda: restarted.app() == (APP_PLAYING, 1), "app shows playing")

    assert restarted.writes() == ["player_queues/play"]
    assert restarted.queue_state() == MusicAssistantState.PLAYING
    assert server.queue(PID).corrected_elapsed_time == pytest.approx(30, abs=1)


async def test_a_session_saved_by_another_version_is_ignored(start_speaker):
    rig = await start_speaker({PID: {"current": {"item_id": "x", "old": 1}}})

    await rig.play_album(0)

    assert rig.plays()[0] == ("replace", [uri("101")])
    assert rig.app() == (APP_PLAYING, 1)
