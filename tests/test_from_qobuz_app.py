"""Playing from the Qobuz app through Music Assistant: an album from start to end, the app's
commands, tracks or players that cannot play, a group, other Qobuz devices and a second app.
"""

import pytest
from music_assistant_models.enums import PlaybackState as MusicAssistantState
from music_assistant_models.enums import PlayerType, QueueOption, RepeatMode

from qobuz_proxy.backends.music_assistant import backend as backend_module
from tests.fake_qobuz import (
    ACTION_NEXT,
    APP_PAUSED,
    APP_PLAYING,
    APP_STOPPED,
    album_state,
    make_tokens,
    set_active,
    set_state,
    set_volume,
    uri,
)
from tests.rig import KITCHEN

# Read at import, before the autouse fixture shrinks them for every test
PRODUCTION_JUMP_WAIT_SECONDS = backend_module.JUMP_WAIT_SECONDS
PRODUCTION_VOLUME_SETTLE_SECONDS = backend_module.VOLUME_SETTLE_SECONDS


async def test_album_starts_in_music_assistant_with_the_next_track_queued_behind_it(rig):
    await rig.play_album(0)
    await rig.wait_armed("102")

    assert rig.plays() == [("replace", [uri("101")]), ("replace_next", [uri("102")])]
    assert rig.server.track_ids(KITCHEN) == ["101", "102"]
    assert rig.app() == (APP_PLAYING, 1)


async def test_album_plays_to_the_end_once_in_order_and_then_music_assistant_is_left_alone(rig):
    rig.server.queue(KITCHEN).repeat_mode = RepeatMode.ALL
    rig.server.queue(KITCHEN).autoplay_enabled = True
    rig.server.queue(KITCHEN).is_dynamic = True  # refuses repeat changes until it is replaced
    rig.server.library_track_ids = {"102"}  # comes back as an item of the user's library

    await rig.play_album(0)
    await rig.wait_armed("102")
    await rig.finish_track()
    await rig.wait_for(lambda: rig.app() == (APP_PLAYING, 2), "app shows track 2")
    await rig.wait_armed("103")
    await rig.finish_track()
    await rig.wait_for(lambda: rig.app() == (APP_PLAYING, 3), "app shows track 3")
    await rig.finish_track()
    assert rig.server.mark_ended(KITCHEN)  # Music Assistant ends the queue instead of repeating
    await rig.wait_for(lambda: rig.app()[0] == APP_STOPPED, "app shows stopped")
    await rig.deliver(set_state(APP_STOPPED, track=("103", 3), pos=0))  # the app's own stop
    await rig.idle(0.3)

    assert rig.shown_queue_items() == [1, 2, 3]
    assert rig.plays() == [
        ("replace", [uri("101")]),
        ("replace_next", [uri("102")]),
        ("replace_next", [uri("103")]),
    ]
    # Music Assistant moved on by itself each time: no stop, restart, skip or seek
    assert set(rig.writes()) == {
        "player_queues/play_media",
        "player_queues/repeat",
        "player_queues/autoplay",
    }
    assert rig.server.queue(KITCHEN).repeat_mode == RepeatMode.OFF
    assert not rig.server.queue(KITCHEN).autoplay_enabled
    assert rig.queue_state() == MusicAssistantState.IDLE


async def test_repeat_turned_back_on_in_music_assistant_goes_off_when_the_speaker_is_picked_again(
    rig,
):
    await rig.play_album(0)
    await rig.deliver(album_state(0, APP_STOPPED))
    rig.server.queue(KITCHEN).repeat_mode = RepeatMode.ALL

    await rig.select()
    await rig.play_album(0)

    assert rig.server.queue(KITCHEN).repeat_mode == RepeatMode.OFF


async def test_pause_and_resume_in_the_app_pause_music_assistant_and_resume_it_where_it_was(rig):
    await rig.play_album(0)
    await rig.wait_armed("102")
    rig.server.advance_time(KITCHEN, 30)
    rig.server.clear_logs()

    # The app sends the position it shows with each command: Music Assistant is already there
    await rig.deliver(album_state(0, APP_PAUSED, pos=30_000))
    await rig.wait_for(lambda: rig.app() == (APP_PAUSED, 1), "app shows paused")
    assert rig.queue_state() == MusicAssistantState.PAUSED
    rig.server.auto_stop_after_pause(KITCHEN)  # Music Assistant stops a queue paused for 30 s
    await rig.server.settle()
    await rig.idle(1.6)  # longer than the player's three 0.5 s checks for a stop
    assert rig.app() == (APP_PAUSED, 1)
    await rig.deliver(album_state(0, APP_PLAYING, pos=30_500))
    await rig.wait_for(lambda: rig.app() == (APP_PLAYING, 1), "app shows playing")

    assert rig.writes() == ["player_queues/pause", "player_queues/play"]
    assert rig.queue_state() == MusicAssistantState.PLAYING
    assert rig.queue_position() == pytest.approx(30, abs=1)


async def test_another_track_picked_while_paused_stops_music_assistant_at_once(rig, monkeypatch):
    monkeypatch.setattr(backend_module, "JUMP_WAIT_SECONDS", PRODUCTION_JUMP_WAIT_SECONDS)
    await rig.play_album(0)
    await rig.wait_armed("102")
    await rig.deliver(album_state(0, APP_PAUSED))
    await rig.wait_for(lambda: rig.queue_state() == MusicAssistantState.PAUSED, "paused")

    await rig.deliver(album_state(2, APP_PAUSED))  # the user picks track 3, still paused
    assert rig.queue_state() == MusicAssistantState.IDLE

    await rig.deliver(album_state(2, APP_PLAYING))
    await rig.wait_for(
        lambda: rig.queue_track() == "103" and rig.app() == (APP_PLAYING, 3), "track 3"
    )


async def test_a_seek_in_the_app_reaches_music_assistant_once_even_if_the_app_repeats_it(rig):
    await rig.play_album(0)
    await rig.wait_armed("102")
    rig.server.clear_logs()

    await rig.deliver(album_state(0, APP_PLAYING, pos=90_000))
    # The app sends the seek again while the speaker still reports where it was before
    rig.server.device_reports(KITCHEN, elapsed=0)
    await rig.server.settle()
    await rig.deliver(album_state(0, APP_PLAYING, pos=90_000))

    assert rig.server.calls("player_queues/seek") == [{"queue_id": KITCHEN, "position": 90}]
    assert rig.queue_state() == MusicAssistantState.PLAYING
    assert rig.app() == (APP_PLAYING, 1)
    assert rig.app_position() == pytest.approx(90_000, abs=1_500)


async def test_after_a_pause_in_music_assistant_a_seek_from_the_app_leaves_it_paused(rig):
    await rig.play_album(0)
    await rig.wait_armed("102")
    rig.server.ui_pause(KITCHEN)
    await rig.wait_for(lambda: rig.app() == (APP_PAUSED, 1), "app shows paused")

    await rig.deliver(album_state(0, APP_PAUSED, pos=90_000))
    await rig.idle(0.3)

    assert rig.queue_state() == MusicAssistantState.PAUSED
    assert rig.queue_position() == pytest.approx(90, abs=1)
    assert rig.app() == (APP_PAUSED, 1)


async def test_volume_from_the_app_reaches_music_assistant_and_the_latest_of_a_burst_wins(
    rig, monkeypatch
):
    monkeypatch.setattr(backend_module, "VOLUME_SETTLE_SECONDS", PRODUCTION_VOLUME_SETTLE_SECONDS)
    await rig.deliver(set_volume(10), set_volume(20), set_volume(30), set_volume(35))
    await rig.wait_for(lambda: rig.cloud.volumes[-1] == 35, "app shows 35%")

    assert rig.volumes_set() == [10, 35]  # the first at once, then only the latest
    assert rig.server.players[KITCHEN].volume_level == 35

    # Music Assistant reports a step of the burst late: the app's slider does not jump back
    shown = len(rig.cloud.volumes)
    rig.server.set_player_volume(KITCHEN, 20)
    await rig.idle(0.1)
    assert rig.cloud.volumes[shown:] == []


async def test_next_in_the_app_jumps_to_the_track_already_queued_and_previous_goes_back(rig):
    await rig.play_album(0)
    await rig.wait_armed("102")
    queued = rig.server.item_ids(KITCHEN)[1]
    rig.server.clear_logs()

    await rig.deliver(album_state(1))
    await rig.wait_for(
        lambda: rig.queue_track() == "102" and rig.app() == (APP_PLAYING, 2), "track 2"
    )
    assert [call["index"] for call in rig.server.calls("player_queues/play_index")] == [queued]
    assert "player_queues/stop" not in rig.writes()
    assert ("replace", [uri("102")]) not in rig.plays()

    rig.server.clear_logs()
    await rig.deliver(album_state(0))
    await rig.wait_for(
        lambda: (
            rig.queue_track() == "101"
            and rig.queue_state() == MusicAssistantState.PLAYING
            and rig.app() == (APP_PLAYING, 1)
        ),
        "track 1 again",
    )
    assert rig.plays()[0] == ("replace", [uri("101")])


async def test_an_album_already_playing_in_music_assistant_is_continued_not_queued_again(rig):
    # Music Assistant plays the album and the speaker has already buffered track 2
    rig.server.start_playing(KITCHEN, ["101", "102"])
    rig.server.buffer_next(KITCHEN)
    await rig.server.settle()

    await rig.play_album(0)
    await rig.wait_armed("102")
    assert rig.plays() == []
    assert rig.server.track_ids(KITCHEN) == ["101", "102"]

    await rig.finish_track()
    await rig.wait_for(lambda: rig.app() == (APP_PLAYING, 2), "app shows track 2")
    assert rig.queue_track() == "102"


@pytest.mark.parametrize("buffered", [False, True], ids=["queued", "already-buffered"])
async def test_a_next_track_removed_in_the_app_is_skipped(rig, buffered):
    await rig.play_album(0)
    await rig.wait_armed("102")
    if buffered:
        rig.server.buffer_next(KITCHEN)  # Music Assistant keeps the item the speaker fetches

    await rig.deliver(set_state(next_track=("103", 3)))  # the user removes track 2 in the app
    await rig.wait_armed("103")
    await rig.finish_track()
    await rig.wait_for(lambda: rig.app() != (APP_PLAYING, 1), "the app moves on")

    assert rig.app() == (APP_PLAYING, 3)
    assert rig.queue_track() == "103"
    assert rig.shown_queue_items() == [1, 3]


async def test_the_last_track_removed_in_the_app_after_the_speaker_buffered_it_ends_the_album(rig):
    await rig.play_album(1)
    await rig.wait_armed("103")
    rig.server.buffer_next(KITCHEN)

    await rig.deliver(set_state())  # the user removes track 3, the last one, in the app
    await rig.finish_track()

    # Not "stopped on track 2": Music Assistant stops at track 3 and the app's queue is done
    await rig.wait_for(lambda: rig.app() == (APP_STOPPED, 0), "app shows the album finished")
    assert rig.queue_state() == MusicAssistantState.IDLE


async def test_tracks_added_in_music_assistant_are_left_alone_and_play_after_the_current_one(rig):
    await rig.play_album(0)
    await rig.wait_armed("102")
    # The user adds a track with "Add to queue" in Music Assistant
    rig.server.start_playing(KITCHEN, ["777"], option=QueueOption.ADD)
    await rig.server.settle()
    rig.server.clear_logs()

    await rig.finish_track()
    await rig.wait_for(lambda: rig.app() == (APP_PLAYING, 2), "app shows track 2")
    await rig.finish_track()
    await rig.wait_for(lambda: rig.queue_track() == "777", "Music Assistant plays the added track")

    assert rig.writes() == []
    assert rig.server.track_ids(KITCHEN) == ["101", "102", "777"]
    assert rig.queue_state() == MusicAssistantState.PLAYING


async def test_region_blocked_track_is_skipped_and_never_sent_to_music_assistant(rig):
    rig.metadata.unavailable = {"102"}

    await rig.play_album(0)
    await rig.finish_track()
    await rig.wait_for(
        lambda: rig.queue_track() == "103" and rig.app() == (APP_PLAYING, 3), "track 3"
    )

    assert ACTION_NEXT in rig.cloud.actions  # the speaker asked the app's queue to skip
    assert all(uri("102") not in media for _, media in rig.plays())
    assert rig.queue_state() == MusicAssistantState.PLAYING


async def test_track_music_assistant_cannot_play_is_skipped_and_the_album_continues(rig):
    rig.server.unplayable_track_ids = {"102"}

    await rig.play_album(0)
    await rig.finish_track()
    await rig.wait_for(
        lambda: rig.queue_track() == "103" and rig.app() == (APP_PLAYING, 3), "track 3"
    )

    assert ACTION_NEXT in rig.cloud.actions
    # Tried once as the next track and once when its turn came, never again
    tries = [option for option, media in rig.plays() if uri("102") in media]
    assert tries == ["replace_next", "replace"]
    assert rig.queue_state() == MusicAssistantState.PLAYING


async def test_an_unavailable_player_plays_nothing_and_does_not_skip_through_the_album(rig):
    rig.server.update_player(KITCHEN, available=False)

    await rig.deliver(album_state(0))
    await rig.wait_for(lambda: rig.app()[0] == APP_STOPPED, "app shows stopped")

    assert rig.cloud.actions == []  # unlike a track Music Assistant cannot play
    assert rig.shown_queue_items() == [1]
    assert rig.queue_state() == MusicAssistantState.IDLE


async def test_a_music_assistant_group_plays_and_takes_the_volume_as_a_whole(server, start_speaker):
    server.add_player("up2", "Living room", volume=40)
    server.add_player("group", "Downstairs", type=PlayerType.GROUP, group_members=[KITCHEN, "up2"])
    rig = await start_speaker(player_id="group", name="Downstairs (MA)")

    await rig.play_album(0)
    assert rig.app() == (APP_PLAYING, 1)

    await rig.deliver(set_volume(50))
    await rig.wait_for(lambda: server.players["up2"].volume_level == 50, "group at 50%")
    assert server.calls("players/cmd/group_volume") == [{"player_id": "group", "volume_level": 50}]
    assert server.players[KITCHEN].volume_level == 50

    await rig.idle(0.1)  # past the window where Music Assistant echoes our own change
    server.set_player_volume("up2", 70)  # changed on the speaker itself: the group is at 60
    await rig.wait_for(lambda: rig.cloud.volumes[-1] == 60, "app shows the group volume")


async def test_another_qobuz_device_stops_music_assistant_once_and_switching_back_resumes(rig):
    await rig.play_album(0)
    await rig.wait_armed("102")
    rig.server.advance_time(KITCHEN, 40)
    rig.server.clear_logs()

    await rig.deliver(set_active(False))
    await rig.wait_for(
        lambda: rig.queue_state() == MusicAssistantState.IDLE, "Music Assistant stopped"
    )
    # The session goes on with the other device, and this speaker still hears about it
    await rig.deliver(album_state(0, APP_PLAYING, pos=41_000))
    await rig.deliver(set_active(False))
    await rig.deliver(set_state(APP_STOPPED, track=("101", 1), pos=41_000))
    await rig.idle(0.3)
    assert rig.writes() == ["player_queues/stop"]

    # Back here, the music goes on where the other device got to, in one stream start
    await rig.deliver(set_active(True), album_state(0, APP_PLAYING, pos=95_000))
    await rig.wait_for(
        lambda: (
            rig.queue_track() == "101"
            and rig.queue_state() == MusicAssistantState.PLAYING
            and rig.app() == (APP_PLAYING, 1)
        ),
        "playing here again",
    )
    assert rig.queue_position() == pytest.approx(95, abs=1)
    assert rig.server.calls("player_queues/seek") == []


async def test_two_qobuz_apps_on_the_same_speaker_both_control_it_without_restarting_the_music(
    rig,
):
    await rig.play_album(0)  # the phone starts the album
    await rig.wait_armed("102")
    rig.server.advance_time(KITCHEN, 40)
    rig.server.clear_logs()

    # The laptop picks the same speaker: it joins the session and the cloud replays the state
    await rig.select(make_tokens(session_id=rig.tokens.session_id, jwt="laptop-jwt"))
    await rig.deliver(album_state(0, APP_PLAYING, pos=40_000))
    await rig.idle(0.3)
    assert rig.writes() == []
    assert rig.queue_track() == "101" and rig.app() == (APP_PLAYING, 1)

    # The laptop pauses, the phone resumes, then either skips to the next track
    await rig.deliver(album_state(0, APP_PAUSED, pos=40_000))
    await rig.wait_for(lambda: rig.queue_state() == MusicAssistantState.PAUSED, "paused")
    await rig.deliver(album_state(0, APP_PLAYING, pos=40_000))
    await rig.wait_for(lambda: rig.queue_state() == MusicAssistantState.PLAYING, "resumed")
    await rig.deliver(album_state(1, APP_PLAYING, pos=0))
    await rig.wait_for(
        lambda: rig.queue_track() == "102" and rig.app() == (APP_PLAYING, 2), "next track"
    )
    await rig.wait_armed("103")

    assert rig.writes() == [
        "player_queues/pause",
        "player_queues/play",
        "player_queues/play_index",
        "player_queues/play_media",
    ]
    assert rig.plays() == [("replace_next", [uri("103")])]
