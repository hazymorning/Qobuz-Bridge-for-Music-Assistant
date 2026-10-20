"""Full-stack Music Assistant scenarios, one test (or small group) per user requirement."""

# ruff: noqa: F403, F405

from __future__ import annotations

import pytest
from music_assistant_models.enums import PlaybackState as MusicAssistantState
from music_assistant_models.enums import PlayerType

from qobuz_proxy.backends.music_assistant import settings
from qobuz_proxy.backends.music_assistant.connection import ConnectionState
from qobuz_proxy.backends.types import PlaybackState
from tests.fake_music_assistant import FakeMusicAssistantServer
from qobuz_proxy.connect.types import ConnectTokens, JWTConnectToken
from tests.fake_qobuz import *
from tests.rig import *
from tests.rig import BACKEND_LOGGER, PID, Rig, assert_no_backend_errors, make_server  # noqa: F401


class TestAlbumStart:
    async def test_play_replaces_music_assistant_queue_reports_playing_and_queues_next_gaplessly(
        self, rig
    ):
        await rig.play_album(0)
        await rig.wait_armed("102")

        assert rig.plays()[:2] == [("replace", [uri("101")]), ("replace_next", [uri("102")])]
        assert len(rig.plays()) == 2
        assert rig.server.track_ids(PID) == ["101", "102"]
        assert rig.queue_track() == "101"
        assert rig.app() == (APP_PLAYING, 1)
        assert rig.player.state == PlaybackState.PLAYING
        # Gapless: MA itself hands the next item to the speaker, the proxy sends no stop/play
        assert "player_queues/stop" not in rig.writes()
        assert "player_queues/play_index" not in rig.writes()


class TestAlbumToTheEnd:
    async def test_three_tracks_play_once_each_in_order_and_stop_at_the_end(self, rig):
        await rig.play_album(0)
        await rig.wait_armed("102")

        await rig.finish_track()
        await rig.wait_for(lambda: rig.app() == (APP_PLAYING, 2), "app shows track 2")
        assert rig.queue_track() == "102"
        await rig.wait_armed("103")

        await rig.finish_track()
        await rig.wait_for(lambda: rig.app() == (APP_PLAYING, 3), "app shows track 3")
        assert rig.queue_track() == "103"

        await rig.finish_track()  # last track: the speaker goes idle
        assert rig.server.mark_ended(PID)
        await rig.wait_for(lambda: rig.app()[0] == APP_STOPPED, "app shows stopped")
        await rig.idle(0.6)

        assert rig.player.state == PlaybackState.STOPPED
        assert rig.player.current_track is None  # a natural end of the queue, not a stop
        assert rig.server.track_ids(PID) == ["101", "102", "103"]
        assert rig.plays() == [
            ("replace", [uri("101")]),
            ("replace_next", [uri("102")]),
            ("replace_next", [uri("103")]),
        ]
        # The app followed every track once, in order (no repeat of the album)
        shown = [r.currentQueueItemId for r in rig.cloud.reports if r.currentQueueItemId]
        assert [
            queue_id for i, queue_id in enumerate(shown) if i == 0 or shown[i - 1] != queue_id
        ] == [1, 2, 3]
        assert not {"player_queues/stop", "player_queues/play_index", "player_queues/play"} & set(
            rig.writes()
        )
        assert rig.queue_state() == MusicAssistantState.IDLE


class TestAppCommands:
    async def test_pause_resume_seek_and_volume_become_music_assistant_commands(self, rig):
        await rig.play_album(0)
        await rig.wait_armed("102")
        rig.server.advance_time(PID, 30)
        rig.server.clear_logs()

        # Pause (the app sends the position it shows: MA is already there, so no seek)
        await rig.deliver(album_state(0, APP_PAUSED, pos=30_000))
        await rig.wait_for(lambda: rig.queue_state() == MusicAssistantState.PAUSED, "MA paused")
        assert rig.writes() == ["player_queues/pause"]
        assert rig.app() == (APP_PAUSED, 1)

        rig.server.clear_logs()
        await rig.deliver(album_state(0, APP_PLAYING, pos=30_000))
        await rig.wait_for(lambda: rig.queue_state() == MusicAssistantState.PLAYING, "MA playing")
        assert rig.writes() == ["player_queues/play"]
        assert rig.app() == (APP_PLAYING, 1)

        rig.server.clear_logs()
        await rig.deliver(album_state(0, APP_PLAYING, pos=90_000))
        await rig.wait_for(lambda: rig.server.calls("player_queues/seek"), "MA seek")
        await rig.idle(0.1)
        assert rig.server.calls("player_queues/seek") == [{"queue_id": PID, "position": 90}]
        assert rig.server.queue(PID).corrected_elapsed_time == pytest.approx(90, abs=1)
        assert rig.queue_state() == MusicAssistantState.PLAYING
        assert rig.app()[0] == APP_PLAYING
        assert rig.app_position() == pytest.approx(90_000, abs=1_500)

        rig.server.clear_logs()
        await rig.deliver(set_volume(35))
        assert rig.server.calls("players/cmd/group_volume") == [
            {"player_id": PID, "volume_level": 35}
        ]
        assert rig.server.players[PID].volume_level == 35
        assert rig.cloud.volumes[-1] == 35

    async def test_next_jumps_to_the_queued_item_and_previous_replaces_the_queue(self, rig):
        await rig.play_album(0)
        await rig.wait_armed("102")
        queued = rig.server.item_ids(PID)[1]
        rig.server.clear_logs()

        await rig.deliver(album_state(1))  # "next" in the app
        await rig.wait_for(
            lambda: rig.queue_track() == "102" and rig.app() == (APP_PLAYING, 2), "track 2"
        )
        jumps = rig.server.calls("player_queues/play_index")
        assert [(c["queue_id"], c["index"]) for c in jumps] == [(PID, queued)]
        assert ("replace", [uri("102")]) not in rig.plays()
        await rig.wait_armed("103")

        rig.server.clear_logs()
        await rig.deliver(album_state(0))  # "previous" in the app
        await rig.wait_for(
            lambda: (
                rig.queue_track() == "101"
                and rig.queue_state() == MusicAssistantState.PLAYING
                and rig.app() == (APP_PLAYING, 1)
            ),
            "track 1 again",
        )
        assert rig.plays()[0] == ("replace", [uri("101")])
        assert rig.server.track_ids(PID)[0] == "101"


class TestMusicAssistantUserInterface:
    async def test_pause_resume_and_next_in_music_assistant_show_in_the_app(self, rig):
        await rig.play_album(0)
        await rig.wait_armed("102")
        rig.server.advance_time(PID, 20)
        rig.server.clear_logs()

        rig.server.ui_pause(PID)
        await rig.wait_for(lambda: rig.app() == (APP_PAUSED, 1), "app shows paused")
        assert rig.player.state == PlaybackState.PAUSED

        rig.server.ui_resume(PID)
        await rig.wait_for(lambda: rig.app() == (APP_PLAYING, 1), "app shows playing")

        rig.server.ui_next(PID)
        await rig.wait_for(lambda: rig.app() == (APP_PLAYING, 2), "app shows track 2")
        assert rig.queue_track() == "102"
        await rig.wait_armed("103")

        # The proxy followed MA and only queued the track after the new one
        assert set(rig.writes()) == {"player_queues/play_media"}
        assert rig.plays() == [("replace_next", [uri("103")])]

    async def test_seek_in_music_assistant_moves_the_apps_progress_bar_within_a_heartbeat(
        self, fast_heartbeat, rig
    ):
        await rig.play_album(0)
        await rig.wait_armed("102")
        rig.server.advance_time(PID, 10)
        await rig.idle(0.2)
        rig.server.clear_logs()

        rig.server.ui_seek(PID, 120)
        await rig.wait_for(
            lambda: rig.app()[0] == APP_PLAYING and abs(rig.app_position() - 120_000) <= 1_500,
            "app shows 2:00",
        )
        assert rig.server.writes == []  # followed, not seeked again

    @pytest.mark.parametrize("left_s", [100, 3], ids=["mid-track", "last-seconds"])
    async def test_stop_in_music_assistant_shows_stopped_and_never_plays_the_next_track(
        self, rig, left_s
    ):
        await rig.play_album(0)
        await rig.wait_armed("102")
        await rig.play_to_near_end(left_s)
        rig.server.clear_logs()

        rig.server.ui_stop(PID)
        await rig.wait_for(lambda: rig.app()[0] == APP_STOPPED, "app shows stopped")
        await rig.idle(0.6)

        assert rig.server.writes == []  # no auto-advance into the next track
        assert rig.queue_state() == MusicAssistantState.IDLE and rig.queue_track() == "101"
        assert rig.player.current_track.track_id == "101"  # stopped, not finished
        assert rig.ws.is_renderer_active

    async def test_after_a_stop_in_music_assistant_the_proxy_never_takes_music_assistant_back(
        self, rig
    ):
        await rig.play_album(0)
        await rig.wait_armed("102")
        rig.server.advance_time(PID, 30)
        rig.server.ui_stop(PID)
        await rig.wait_for(lambda: rig.app()[0] == APP_STOPPED, "app shows stopped")
        rig.server.clear_logs()

        # The user carries on in MA: resume, then MA plays through the queue on its own
        rig.server.ui_resume(PID)
        await rig.idle(0.2)
        rig.server.advance_time(PID, TRACK_S - 31)
        rig.server.finish_current_track(PID)
        await rig.idle(0.2)
        assert rig.queue_track() == "102"
        rig.server.advance_time(PID, TRACK_S - 1)
        rig.server.finish_current_track(PID)
        rig.server.mark_ended(PID)
        await rig.idle(0.6)

        assert rig.server.writes == []  # no re-queue, no replay of track 2, no stop
        assert rig.player.state == PlaybackState.STOPPED

    async def test_previous_in_music_assistant_shows_stopped_but_keeps_the_session(self, rig):
        await rig.play_album(0)
        await rig.wait_armed("102")
        await rig.finish_track()
        await rig.wait_for(lambda: rig.app() == (APP_PLAYING, 2), "app shows track 2")
        await rig.idle(0.2)  # past the start grace of track 2
        rig.server.clear_logs()

        rig.server.ui_previous(PID)  # within 5 s of the start: MA goes back to track 1
        await rig.wait_for(lambda: rig.app()[0] == APP_STOPPED, "app shows stopped")
        await rig.idle(0.6)

        assert rig.queue_track() == "101" and rig.queue_state() == MusicAssistantState.PLAYING
        assert rig.server.writes == []  # no fight-back
        assert rig.ws.is_renderer_active  # still selected: play in the app works again
        assert not rig.ws._external_playback


async def play_something_else_in_music_assistant(rig: Rig) -> None:
    await rig.play_album(0)
    await rig.wait_armed("102")
    await rig.idle(0.2)  # past the start grace
    rig.server.clear_logs()
    rig.server.user_plays_something_else(PID, "Jazz Radio")
    await rig.wait_for(lambda: rig.ws._external_playback, "Qobuz session released")


class TestExternalPlaybackInMusicAssistant:
    async def test_other_music_releases_the_qobuz_session(self, rig):
        await play_something_else_in_music_assistant(rig)

        assert rig.player.state == PlaybackState.STOPPED
        assert not rig.ws.is_renderer_active
        assert rig.cloud.sockets[-1].closed.is_set()  # left the session: the app shows stopped
        assert rig.discovery is not None and rig.discovery.cleared == 1
        assert rig.server.writes == []
        queue = rig.server.queue(PID)
        assert (
            queue.current_item.name == "Jazz Radio" and queue.state == MusicAssistantState.PLAYING
        )

    async def test_no_music_assistant_writes_after_a_external_playback_even_for_replayed_snapshots(
        self, rig
    ):
        await play_something_else_in_music_assistant(rig)

        # The cloud replays the old session state (dropped by the released socket) ...
        await rig.deliver(set_active(True), album_state(0, APP_PLAYING, pos=4_000))
        # ... and even if a snapshot reached the player, the backend refuses it
        await rig.player.apply_remote_state(
            track_id="101", queue_item_id=1, position_ms=4_000, playing_state=APP_PLAYING
        )
        await rig.player.apply_remote_state(
            track_id="102", queue_item_id=2, position_ms=0, playing_state=APP_PAUSED
        )
        await rig.player.set_volume(10)
        await rig.idle(0.6)  # the player's monitor keeps polling the backend

        assert rig.server.writes == []
        assert rig.server.queue(PID).current_item.name == "Jazz Radio"
        assert rig.server.players[PID].volume_level != 10

    async def test_selecting_the_device_again_takes_the_player_back(self, rig):
        await play_something_else_in_music_assistant(rig)
        assert rig.server.queue(PID).state == MusicAssistantState.PLAYING  # the radio still plays

        await rig.select()
        await rig.play_album(0)

        assert rig.plays()[0] == ("replace", [uri("101")])
        await rig.wait_for(lambda: rig.app() == (APP_PLAYING, 1), "app shows playing")
        assert rig.ws.is_renderer_active


class TestEndOfQueue:
    async def test_last_track_ends_stopped_and_music_assistant_is_left_alone(self, rig):
        await rig.play_album(2)  # the album's last track: no next
        await rig.finish_track()
        await rig.wait_for(lambda: rig.app()[0] == APP_STOPPED, "app shows stopped")
        rig.server.clear_logs()

        assert rig.server.mark_ended(PID)
        await rig.deliver(set_state(APP_STOPPED, track=("103", 3), pos=0))  # the app's stop
        await rig.idle(0.6)

        assert rig.server.writes == []
        assert rig.player.state == PlaybackState.STOPPED
        assert rig.queue_state() == MusicAssistantState.IDLE and rig.server.queue(PID).ended
        assert rig.plays() == []

    async def test_repeat_and_autoplay_in_music_assistant_are_turned_off_so_the_album_ends(
        self, server, rig
    ):
        from music_assistant_models.enums import RepeatMode

        server.queue(PID).repeat_mode = RepeatMode.ALL
        server.queue(PID).autoplay_enabled = True
        await rig.play_album(2)

        assert server.queue(PID).repeat_mode == RepeatMode.OFF
        assert not server.queue(PID).autoplay_enabled
        await rig.finish_track()
        await rig.wait_for(lambda: rig.app()[0] == APP_STOPPED, "app shows stopped")
        assert rig.server.track_ids(PID) == ["103"]
        assert rig.queue_state() == MusicAssistantState.IDLE


class TestUnavailableTracks:
    async def test_region_blocked_track_is_skipped_and_never_sent_to_music_assistant(self, rig):
        rig.metadata.unavailable = {"102"}
        await rig.play_album(0)
        await rig.idle(0.6)  # the player tries to queue track 2: no URL, so nothing reaches MA
        assert all(uri("102") not in media for _, media in rig.plays())

        await rig.finish_track()
        await rig.wait_for(
            lambda: (
                rig.queue_track() == "103"
                and rig.queue_state() == MusicAssistantState.PLAYING
                and rig.app() == (APP_PLAYING, 3)
            ),
            "track 3 playing",
        )

        assert ACTION_NEXT in rig.cloud.actions  # the renderer asked the app's queue to skip
        assert all(uri("102") not in media for _, media in rig.plays())
        assert "102" not in rig.server.track_ids(PID)

    async def test_track_music_assistant_cannot_play_is_skipped_and_the_album_continues(self, rig):
        rig.server.unplayable_track_ids = {"102"}
        await rig.play_album(0)
        await rig.idle(0.6)

        await rig.finish_track()
        await rig.wait_for(
            lambda: (
                rig.queue_track() == "103"
                and rig.queue_state() == MusicAssistantState.PLAYING
                and rig.app() == (APP_PLAYING, 3)
            ),
            "track 3 playing",
        )

        assert ACTION_NEXT in rig.cloud.actions
        assert "102" not in rig.server.track_ids(PID)
        assert rig.player.state == PlaybackState.PLAYING


@pytest.fixture
def group_server() -> FakeMusicAssistantServer:
    server = FakeMusicAssistantServer()
    server.add_qobuz_provider()
    server.add_player("m1", "Kitchen", volume=20)
    server.add_player("m2", "Living room", volume=40)
    server.add_player(
        "grp",
        "Downstairs",
        type=PlayerType.GROUP,
        provider="player_group",
        group_members=["m1", "m2"],
    )
    return server


class TestGroupPlayer:
    async def test_group_plays_and_volume_goes_through_group_volume(
        self, group_server, monkeypatch
    ):
        rig = await Rig(group_server, monkeypatch, player_id="grp", name="Downstairs (MA)").start()
        try:
            await rig.select()
            await rig.play_album(0)
            assert rig.plays()[0] == ("replace", [uri("101")])
            assert group_server.calls("player_queues/play_media")[0]["queue_id"] == "grp"
            await rig.wait_for(lambda: rig.app() == (APP_PLAYING, 1), "app shows playing")

            group_server.clear_logs()
            await rig.deliver(set_volume(40))
            assert group_server.calls("players/cmd/group_volume") == [
                {"player_id": "grp", "volume_level": 40}
            ]
            assert group_server.players["m1"].volume_level == 40
            assert group_server.players["m2"].volume_level == 40

            await rig.idle(0.1)  # past the window where MA echoes our own change
            group_server.set_player_volume("grp", 55)  # changed in MA's UI
            await rig.wait_for(
                lambda: rig.cloud.volumes and rig.cloud.volumes[-1] == 55, "app shows 55%"
            )
            assert rig.player._volume == 55
            assert "players/cmd/group_volume" not in [n for n, _ in group_server.writes][1:]
        finally:
            await rig.close()


class TestMusicAssistantRestart:
    async def test_quick_restart_shows_stopped_and_play_works_after_reconnect(self, rig, caplog):
        await rig.play_album(0)
        await rig.wait_armed("102")
        rig.server.advance_time(PID, 30)
        await rig.idle(0.2)

        rig.server.restart()
        await rig.wait_for(lambda: not rig.conn.is_connected, "connection lost")
        rig.server.come_back()
        await rig.wait_for(lambda: rig.conn.is_connected, "reconnected")
        await rig.wait_for(lambda: rig.app()[0] == APP_STOPPED, "app shows stopped")
        assert rig.player.state == PlaybackState.STOPPED
        assert rig.app_position() == pytest.approx(30_000, abs=2_000)
        assert rig.ws.is_renderer_active  # still selected

        rig.server.clear_logs()
        await rig.deliver(album_state(0, APP_PLAYING, pos=30_000))  # play where it stopped
        await rig.wait_for(
            lambda: (
                rig.queue_track() == "101"
                and rig.queue_state() == MusicAssistantState.PLAYING
                and rig.app() == (APP_PLAYING, 1)
            ),
            "playing again",
        )
        assert rig.server.queue(PID).corrected_elapsed_time == pytest.approx(30, abs=2)
        assert_no_backend_errors(caplog)

    async def test_long_outage_shows_stopped_after_the_grace_and_recovers(self, rig, caplog):
        await rig.play_album(0)
        await rig.wait_armed("102")
        rig.server.advance_time(PID, 30)

        rig.server.restart()  # and stays down
        await rig.wait_for(lambda: rig.app()[0] == APP_STOPPED, "app shows stopped")
        assert not rig.conn.is_connected
        assert rig.player.state == PlaybackState.STOPPED

        rig.server.come_back()
        await rig.wait_for(lambda: rig.conn.is_connected, "reconnected")
        rig.server.clear_logs()
        await rig.play_album(0)
        await rig.wait_for(lambda: rig.app() == (APP_PLAYING, 1), "app shows playing")
        assert_no_backend_errors(caplog)


class TestTokenRevoked:
    async def test_revoked_token_stops_reconnecting_and_the_app_shows_stopped(self, rig):
        await rig.play_album(0)
        before = len(rig.server.created_clients)

        rig.server.revoke_token(rig.server.token)
        await rig.wait_for(
            lambda: rig.conn.status.state == ConnectionState.AUTH_FAILED, "auth failed"
        )
        assert rig.player.state == PlaybackState.PLAYING  # a short outage is not a stop yet
        await rig.wait_for(lambda: rig.app()[0] == APP_STOPPED, "app shows stopped")
        assert rig.player.state == PlaybackState.STOPPED

        await rig.idle(0.5)  # 25 reconnect delays
        assert len(rig.server.created_clients) - before == 1  # one attempt, then it gave up
        assert rig.conn.status.state == ConnectionState.AUTH_FAILED


class TestAddonRestart:
    async def _restart_mid_play(self, server, monkeypatch, tmp_path) -> tuple[Rig, list[str]]:
        """Play, stop the add-on (MA keeps playing), start a new process, re-select."""
        first = await Rig(server, monkeypatch).start()
        await first.select()
        await first.play_album(0)
        await first.wait_armed("102")
        server.advance_time(PID, 60)
        await first.close()  # the add-on stops; MA keeps playing

        assert "player_queues/stop" not in first.writes()
        assert server.queue(PID).state == MusicAssistantState.PLAYING
        items_before = server.item_ids(PID)
        # A new process reads the remembered session from the settings file
        path = tmp_path / "music_assistant.json"
        settings.save(path, settings.Settings(sessions=first.sessions))
        server.clear_logs()

        second = await Rig(server, monkeypatch, sessions=settings.load(path).sessions).start()
        await second.select()
        # After the reconnect the Qobuz server replays its snapshot from before (paused at an
        # old position), then the app plays
        await second.deliver(album_state(0, APP_PAUSED, pos=4_000))
        await second.deliver(album_state(0, APP_PLAYING, pos=4_000))
        await second.wait_for(lambda: second.player.state == PlaybackState.PLAYING, "playing")
        return second, items_before

    async def test_restart_mid_play_takes_over_what_music_assistant_plays_without_restarting_it(
        self, fast_heartbeat, server, monkeypatch, tmp_path
    ):
        second, _ = await self._restart_mid_play(server, monkeypatch, tmp_path)
        try:
            await second.wait_for(
                lambda: second.app()[0] == APP_PLAYING and second.app_position() >= 59_000,
                "the app follows MA's position",
            )
            # No restart of the track, no seek back to the stale 0:04, no stop or pause
            assert all(uri("101") not in media for _, media in second.plays())
            assert not {
                "player_queues/stop",
                "player_queues/pause",
                "player_queues/play",
                "player_queues/seek",
                "player_queues/play_index",
                "player_queues/clear",
            } & set(second.writes())
            assert server.queue(PID).corrected_elapsed_time >= 60
            assert second.queue_track() == "101"
            assert server.queue(PID).state == MusicAssistantState.PLAYING

            # The album goes on with the next track
            await second.finish_track()
            await second.wait_for(lambda: second.app() == (APP_PLAYING, 2), "app shows track 2")
            assert second.queue_track() == "102"
        finally:
            await second.close()

    async def test_restart_keeps_the_next_track_music_assistant_already_has_queued(
        self, fast_heartbeat, server, monkeypatch, tmp_path
    ):
        second, items_before = await self._restart_mid_play(server, monkeypatch, tmp_path)
        try:
            await second.wait_for(lambda: second.player._gapless_armed, "next track armed")
            await second.idle(0.6)
            assert second.writes() == []
            assert server.item_ids(PID) == items_before
        finally:
            await second.close()


class TestSwitchQobuzDevice:
    async def test_switching_away_stops_music_assistant_once_and_switching_back_plays(self, rig):
        await rig.play_album(0)
        await rig.wait_armed("102")
        rig.server.advance_time(PID, 40)
        rig.server.clear_logs()

        await rig.deliver(set_active(False))
        await rig.wait_for(lambda: rig.queue_state() == MusicAssistantState.IDLE, "MA stopped")
        assert rig.writes() == ["player_queues/stop"]

        # The session goes on with the other device
        await rig.deliver(album_state(0, APP_PLAYING, pos=41_000))
        await rig.deliver(set_active(False))
        await rig.deliver(set_state(APP_STOPPED, track=("101", 1), pos=41_000))
        await rig.idle(0.6)
        assert rig.writes() == ["player_queues/stop"]
        assert rig.player.state == PlaybackState.STOPPED

        await rig.deliver(set_active(True), album_state(0, APP_PLAYING, pos=0))
        await rig.wait_for(
            lambda: (
                rig.queue_track() == "101"
                and rig.queue_state() == MusicAssistantState.PLAYING
                and rig.app() == (APP_PLAYING, 1)
            ),
            "playing here again",
        )


class TestTwoQobuzApps:
    async def test_phone_and_laptop_both_control_the_player_without_restarting_it(self, rig):
        await rig.play_album(0)  # the phone starts the album
        await rig.wait_armed("102")
        rig.server.advance_time(PID, 40)
        rig.server.clear_logs()

        # The laptop picks the same speaker: a new discovery connect into the same session,
        # and the cloud replays where the music is
        laptop = ConnectTokens(
            session_id=rig.tokens.session_id,
            ws_token=JWTConnectToken(jwt="laptop-jwt", exp=9_999_999_999, endpoint="wss://q/ws"),
        )
        await rig.select(laptop)
        await rig.deliver(album_state(0, APP_PLAYING, pos=40_000))
        await rig.idle(0.3)
        assert rig.writes() == []  # no restart, no seek: MA just plays on
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
        assert rig.writes()[:3] == [
            "player_queues/pause",
            "player_queues/play",
            "player_queues/play_index",  # the next track MA already had queued
        ]
        # Only the following track gets queued; nothing replaces what plays
        assert {c["option"] for c in rig.server.calls("player_queues/play_media")} <= {
            "replace_next"
        }


class TestMusicAssistantUnreachableAtBoot:
    async def test_play_fails_cleanly_while_music_assistant_is_down_and_works_once_it_is_back(
        self, server, monkeypatch, caplog
    ):
        server.reachable = False
        rig = await Rig(server, monkeypatch).start(wait_for_ma=False)
        try:
            assert rig.conn.status.state in (
                ConnectionState.CONNECTING,
                ConnectionState.DISCONNECTED,
            )
            await rig.select()  # the device is visible and selectable anyway
            await rig.deliver(album_state(0))
            await rig.wait_for(lambda: rig.player.state == PlaybackState.ERROR, "player error")
            assert rig.app()[0] == APP_STOPPED
            assert server.command_log == []

            server.reachable = True
            await rig.wait_for(lambda: rig.conn.is_connected, "MA connected")
            await rig.play_album(0)
            await rig.wait_for(lambda: rig.app() == (APP_PLAYING, 1), "app shows playing")
            assert rig.plays()[0] == ("replace", [uri("101")])
            assert_no_backend_errors(caplog)
        finally:
            await rig.close()
