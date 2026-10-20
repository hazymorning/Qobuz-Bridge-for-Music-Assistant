"""What the Qobuz app shows when the player is used in Music Assistant: pause, next, previous,
seek, stop and clear pressed there, the player ending or stalling a track, and external playback.
"""

import pytest
from music_assistant_models.enums import PlaybackState as MusicAssistantState

from tests.fake_qobuz import (
    ACTION_NEXT,
    APP_PAUSED,
    APP_PLAYING,
    APP_STOPPED,
    TRACK_S,
    album_state,
    uri,
)
from tests.rig import PID, Rig


async def _player_goes_idle_for_a_moment(rig: Rig) -> None:
    rig.server.device_reports(PID, MusicAssistantState.IDLE)
    await rig.server.settle()
    rig.server.device_reports(PID, MusicAssistantState.PLAYING)
    await rig.server.settle()


async def test_pause_resume_and_next_in_music_assistant_show_in_the_app(rig):
    await rig.play_album(0)
    await rig.wait_armed("102")
    rig.server.clear_logs()

    rig.server.ui_pause(PID)
    await rig.wait_for(lambda: rig.app() == (APP_PAUSED, 1), "app shows paused")
    rig.server.ui_resume(PID)
    await rig.wait_for(lambda: rig.app() == (APP_PLAYING, 1), "app shows playing")
    rig.server.ui_next(PID)
    await rig.wait_for(lambda: rig.app() == (APP_PLAYING, 2), "app shows track 2")
    await rig.wait_armed("103")

    assert rig.queue_track() == "102"
    # The add-on followed Music Assistant and only queued the track after the new one
    assert rig.writes() == ["player_queues/play_media"]
    assert rig.plays() == [("replace_next", [uri("103")])]


async def test_seek_in_music_assistant_moves_the_progress_shown_in_the_app(fast_heartbeat, rig):
    await rig.play_album(0)
    await rig.wait_armed("102")
    rig.server.clear_logs()

    rig.server.ui_seek(PID, 120)

    await rig.wait_for(
        lambda: rig.app()[0] == APP_PLAYING and abs(rig.app_position() - 120_000) <= 1_500,
        "app shows 2:00",
    )
    assert rig.server.writes == []  # followed, not seeked again


async def test_previous_in_music_assistant_shows_stopped_but_keeps_the_speaker_selected(rig):
    await rig.play_album(0)
    await rig.wait_armed("102")
    rig.server.ui_next(PID)
    await rig.wait_for(lambda: rig.app() == (APP_PLAYING, 2), "app shows track 2")
    await rig.idle(0.2)  # past the start grace of track 2
    rig.server.clear_logs()

    rig.server.ui_previous(PID)  # early in track 2: back to track 1
    await rig.wait_for(lambda: rig.app() == (APP_STOPPED, 2), "app shows stopped")
    await rig.idle(0.2)
    assert rig.queue_track() == "101"
    assert rig.server.writes == []  # no fight-back

    # Still the selected speaker: play in the app works straight away
    await rig.play_album(1)
    await rig.wait_for(lambda: rig.app() == (APP_PLAYING, 2), "app shows track 2 playing")


@pytest.mark.parametrize(
    ("seconds_left", "stop"),
    [
        # So late that it must not pass for the track ending
        (3, lambda server: server.ui_stop(PID)),
        # Its own buttons, or another app: Music Assistant only sees it go idle
        (100, lambda server: server.device_reports(PID, MusicAssistantState.IDLE)),
    ],
    ids=["in-music-assistant-in-the-last-seconds", "on-the-player-mid-track"],
)
async def test_a_stop_outside_the_app_shows_stopped_and_the_album_never_goes_on(
    rig, seconds_left, stop
):
    await rig.play_album(0)
    await rig.wait_armed("102")
    await rig.play_to_near_end(seconds_left)
    rig.server.clear_logs()

    stop(rig.server)
    await rig.wait_for(lambda: rig.app() == (APP_STOPPED, 1), "app shows stopped")
    # Where it stopped, so play in the app resumes there
    assert rig.app_position() == pytest.approx((TRACK_S - seconds_left) * 1000, abs=1_500)
    stopped = len(rig.cloud.reports)

    # Then the user plays on in Music Assistant, which goes through its queue by itself
    rig.server.ui_resume(PID)
    await rig.server.settle()
    rig.server.finish_current_track(PID)
    await rig.idle(0.2)  # track 2 plays a while
    rig.server.finish_current_track(PID)
    rig.server.mark_ended(PID)
    await rig.idle(0.2)

    assert rig.server.writes == []
    assert rig.app() == (APP_STOPPED, 1)
    assert APP_PLAYING not in rig.shown_states(since=stopped)


async def test_clearing_the_queue_in_music_assistant_shows_stopped(rig):
    await rig.play_album(0)
    await rig.wait_armed("102")
    rig.server.clear_logs()

    rig.server.ui_clear(PID)

    await rig.wait_for(lambda: rig.app() == (APP_STOPPED, 1), "app shows stopped")
    assert rig.writes() == []
    assert not rig.has_left_qobuz_session()  # a stop, not external playback


async def test_the_last_track_played_to_its_end_shows_the_album_finished(rig):
    await rig.play_album(2)
    # Paused and resumed in Music Assistant on the way
    rig.server.ui_pause(PID)
    await rig.wait_for(lambda: rig.app() == (APP_PAUSED, 3), "app shows paused")
    rig.server.ui_resume(PID)
    await rig.wait_for(lambda: rig.app() == (APP_PLAYING, 3), "app shows playing")
    await rig.finish_track()

    # Not "stopped on track 3": the app's queue is done
    await rig.wait_for(lambda: rig.app() == (APP_STOPPED, 0), "app shows the album finished")


async def test_short_idle_flaps_never_stop_the_track(rig):
    await rig.play_album(0)
    await rig.wait_armed("102")
    rig.server.clear_logs()

    # A player goes idle for a moment when its stream restarts: right after a seek...
    await rig.deliver(album_state(0, APP_PLAYING, pos=120_000))
    await _player_goes_idle_for_a_moment(rig)
    await rig.idle(0.2)
    # ...and at any other time
    await _player_goes_idle_for_a_moment(rig)
    await rig.idle(0.2)

    assert rig.app() == (APP_PLAYING, 1)
    assert rig.writes() == ["player_queues/seek"]


async def test_a_player_that_never_starts_shows_stopped_instead_of_skipping_the_album(rig):
    await rig.play_album(0)
    await rig.wait_armed("102")
    rig.server.dead_player_ids.add(PID)  # track 1 played, then the speaker stopped responding
    rig.server.clear_logs()

    await rig.deliver(album_state(1))  # next in the app
    await rig.wait_for(lambda: rig.app() == (APP_STOPPED, 2), "app shows stopped")
    await rig.idle(0.2)

    assert rig.app() == (APP_STOPPED, 2)
    assert rig.writes() == ["player_queues/play_index"]  # tried once, nothing skipped to
    assert ACTION_NEXT not in rig.cloud.actions


async def test_a_player_stuck_past_the_end_of_the_track_shows_stopped(rig):
    await rig.play_album(0)
    await rig.wait_armed("102")
    await rig.idle(0.2)  # past the start grace
    rig.server.clear_logs()

    # Music Assistant still says "playing" 30 s past the end: it lost track of the player
    rig.server.device_reports(PID, elapsed=TRACK_S + 30)

    await rig.wait_for(lambda: rig.app() == (APP_STOPPED, 1), "app shows stopped")
    assert rig.server.writes == []


async def test_after_a_stop_in_the_app_music_assistant_is_free_to_play_anything(rig):
    await rig.play_album(0)
    await rig.wait_armed("102")
    await rig.idle(0.2)  # past the start grace
    await rig.deliver(album_state(0, APP_STOPPED))
    await rig.wait_for(lambda: rig.queue_state() == MusicAssistantState.IDLE, "stopped")
    rig.server.clear_logs()
    stopped = len(rig.cloud.reports)

    # Somebody resumes in Music Assistant, which plays on into the track that was queued
    # next, and then puts the radio on
    rig.server.ui_resume(PID)
    await rig.server.settle()
    rig.server.finish_current_track(PID)
    await rig.server.settle()
    rig.server.user_plays_something_else(PID, "Jazz Radio")
    await rig.idle(0.2)

    assert rig.server.writes == []
    assert APP_PLAYING not in rig.shown_states(since=stopped)
    assert not rig.has_left_qobuz_session()  # still selected: the app can play here again


@pytest.mark.parametrize(
    "play_something_else",
    [
        lambda server: server.user_plays_something_else(PID, track_ids=["555"]),
        lambda server: server.switch_source(PID, "spotify"),
    ],
    ids=["qobuz-music-started-in-music-assistant", "another-source"],
)
async def test_external_playback_makes_the_speaker_let_go_without_fighting_back(
    rig, play_something_else
):
    await rig.play_album(0)
    await rig.wait_armed("102")
    rig.server.clear_logs()

    play_something_else(rig.server)
    # Leaving the Qobuz session is what makes the app show the speaker stopped
    await rig.wait_for(rig.has_left_qobuz_session, "the speaker left the session")

    # A snapshot and a volume change the app sent just before can still reach the player
    await rig.player.apply_remote_state(
        track_id="101", queue_item_id=1, position_ms=4_000, playing_state=APP_PLAYING
    )
    await rig.player.set_volume(10)
    await rig.idle(0.2)

    assert rig.server.writes == []


async def test_selecting_the_speaker_again_takes_the_player_back(rig):
    await rig.play_album(0)
    await rig.wait_armed("102")
    rig.server.user_plays_something_else(PID, "Jazz Radio")
    await rig.wait_for(rig.has_left_qobuz_session, "the speaker left the session")
    rig.server.clear_logs()

    await rig.select()
    await rig.play_album(0)

    assert rig.plays()[0] == ("replace", [uri("101")])
    await rig.wait_for(lambda: rig.app() == (APP_PLAYING, 1), "app shows playing")
