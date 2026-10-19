"""Semantics of the fake Music Assistant server (MA 2.10.7 behaviour the backend relies on)."""

from __future__ import annotations

import asyncio
import logging
from collections.abc import AsyncIterator
from typing import Any

import pytest

pytest.importorskip("music_assistant_models")
pytest.importorskip("music_assistant_client")

from music_assistant_client.exceptions import (  # noqa: E402
    CannotConnect,
    ConnectionClosed,
    ConnectionFailed,
    InvalidServerVersion,
    InvalidState,
)
from music_assistant_models.enums import (  # noqa: E402
    EventType,
    MediaType,
    PlaybackState,
    PlayerType,
    QueueOption,
    RepeatMode,
)
from music_assistant_models.errors import (  # noqa: E402
    AuthenticationFailed,
    AuthenticationRequired,
    InsufficientPermissions,
    InvalidCommand,
    InvalidDataError,
    InvalidToken,
    LoginFailed,
    MediaNotFoundError,
    PlayerUnavailableError,
)
from music_assistant_models.event import MassEvent  # noqa: E402

from tests.backends.fake_music_assistant import (  # noqa: E402
    ATTR_PLAY_ACTION_IN_PROGRESS,
    DROP,
    QOBUZ_INSTANCE_ID,
    FakeMAServer,
    FakeMusicAssistantClient,
)

QID = "up1"


def qobuz(track_id: str) -> str:
    return f"qobuz://track/{track_id}"


@pytest.fixture
def server() -> FakeMAServer:
    srv = FakeMAServer()
    srv.add_qobuz_provider()
    srv.add_player(QID, "Kitchen")
    return srv


@pytest.fixture
async def client(server: FakeMAServer) -> AsyncIterator[FakeMusicAssistantClient]:
    ma_client, listen_task = await server.connect_client()
    yield ma_client
    await ma_client.disconnect()
    await asyncio.wait_for(listen_task, 1)


def record(client: FakeMusicAssistantClient) -> list[MassEvent]:
    events: list[MassEvent] = []
    client.subscribe(events.append)
    return events


# ------------------------------------------------------------------------- enqueue options


class TestEnqueueOptions:
    @pytest.mark.parametrize(
        ("option", "expected_tracks", "expected_current"),
        [
            (QueueOption.REPLACE, ["n"], "n"),
            (QueueOption.PLAY, ["a", "n", "b", "c"], "n"),
            (QueueOption.NEXT, ["a", "n", "b", "c"], "a"),
            (QueueOption.REPLACE_NEXT, ["a", "n"], "a"),
            (QueueOption.ADD, ["a", "b", "c", "n"], "a"),
        ],
    )
    async def test_options_on_a_playing_queue(
        self,
        server: FakeMAServer,
        client: FakeMusicAssistantClient,
        option: QueueOption,
        expected_tracks: list[str],
        expected_current: str,
    ) -> None:
        server.start_playing(QID, ["a", "b", "c"])
        await server.settle()

        await client.player_queues.play_media(QID, qobuz("n"), option=option)

        assert server.track_ids(QID) == expected_tracks
        queue = client.player_queues.get(QID)
        assert queue is not None
        assert queue.state == PlaybackState.PLAYING
        assert queue.current_item is not None
        assert queue.current_item.media_item.item_id == expected_current
        assert queue.items == len(expected_tracks)

    async def test_replace_starts_index_zero_from_the_beginning(
        self, server: FakeMAServer, client: FakeMusicAssistantClient
    ) -> None:
        server.start_playing(QID, ["a", "b"])
        server.advance_time(QID, 40)

        await client.player_queues.play_media(QID, qobuz("n"), option=QueueOption.REPLACE)

        queue = server.queue(QID)
        assert queue.current_index == 0
        assert queue.index_in_buffer == 0
        assert queue.elapsed_time == pytest.approx(0, abs=0.5)
        player = client.players.get(QID)
        assert player is not None
        assert player.current_media is not None
        assert player.current_media.queue_item_id == server.item_ids(QID)[0]
        assert player.current_media.uri == f"{QOBUZ_INSTANCE_ID}://track/n"

    async def test_buffered_next_moves_the_insert_point(
        self, server: FakeMAServer, client: FakeMusicAssistantClient
    ) -> None:
        server.start_playing(QID, ["a", "b", "c"])
        buffered = server.buffer_next(QID)
        assert buffered is not None and buffered.media_item.item_id == "b"
        assert server.queue(QID).index_in_buffer == 1

        await client.player_queues.play_media(QID, qobuz("n"), option=QueueOption.REPLACE_NEXT)
        assert server.track_ids(QID) == ["a", "b", "n"]

        await client.player_queues.play_media(QID, qobuz("m"), option=QueueOption.NEXT)
        assert server.track_ids(QID) == ["a", "b", "m", "n"]

    async def test_paused_queue_uses_the_committed_index(self, server: FakeMAServer) -> None:
        server.start_playing(QID, ["a", "b", "c"])
        server.buffer_next(QID)
        server.ui_pause(QID)
        assert server.queue(QID).state == PlaybackState.PAUSED

        server.start_playing(QID, ["n"], QueueOption.REPLACE_NEXT)

        assert server.track_ids(QID) == ["a", "b", "n"]

    async def test_stopped_queue_inserts_after_current_index(self, server: FakeMAServer) -> None:
        server.start_playing(QID, ["a", "b", "c"])
        server.ui_next(QID)
        server.ui_stop(QID)
        queue = server.queue(QID)
        assert queue.state == PlaybackState.IDLE
        assert queue.current_index == 1

        server.start_playing(QID, ["n"], QueueOption.NEXT)

        assert server.track_ids(QID) == ["a", "b", "n", "c"]
        assert server.queue(QID).state == PlaybackState.IDLE

    async def test_add_on_empty_queue_only_moves_the_index(self, server: FakeMAServer) -> None:
        server.start_playing(QID, ["n"], QueueOption.ADD)

        queue = server.queue(QID)
        assert server.track_ids(QID) == ["n"]
        assert queue.current_index == 0
        assert queue.current_item is not None
        assert queue.state == PlaybackState.IDLE
        assert server.players[QID].playback_state == PlaybackState.IDLE

    async def _ended(self, server: FakeMAServer) -> None:
        server.start_playing(QID, ["a"])
        server.finish_current_track(QID)
        assert server.mark_ended(QID) is True
        assert server.queue(QID).ended is True

    async def test_add_on_ended_queue_continues_it(self, server: FakeMAServer) -> None:
        await self._ended(server)

        server.start_playing(QID, ["n"], QueueOption.ADD)

        queue = server.queue(QID)
        assert server.track_ids(QID) == ["a", "n"]
        assert queue.ended is False
        assert queue.current_index == 1
        assert queue.state == PlaybackState.IDLE

    @pytest.mark.parametrize("option", [QueueOption.NEXT, QueueOption.REPLACE_NEXT])
    async def test_ended_queue_is_cleared_first(
        self, server: FakeMAServer, option: QueueOption
    ) -> None:
        await self._ended(server)

        server.start_playing(QID, ["n"], option)

        queue = server.queue(QID)
        assert server.track_ids(QID) == ["n"]
        assert queue.current_index == 0
        assert queue.ended is False
        assert queue.state == PlaybackState.IDLE

    async def test_play_on_ended_queue_plays_only_the_new_item(self, server: FakeMAServer) -> None:
        await self._ended(server)

        server.start_playing(QID, ["n"], QueueOption.PLAY)

        assert server.track_ids(QID) == ["n"]
        assert server.queue(QID).state == PlaybackState.PLAYING

    async def test_unplayable_tracks_are_skipped(
        self, server: FakeMAServer, client: FakeMusicAssistantClient
    ) -> None:
        server.unplayable_track_ids.add("x")

        with pytest.raises(MediaNotFoundError, match="No playable items found"):
            await client.player_queues.play_media(QID, qobuz("x"), option=QueueOption.REPLACE)
        assert server.track_ids(QID) == []

        await client.player_queues.play_media(
            QID, [qobuz("x"), qobuz("y")], option=QueueOption.REPLACE
        )
        assert server.track_ids(QID) == ["y"]

    async def test_no_qobuz_provider_means_nothing_playable(self) -> None:
        server = FakeMAServer()
        server.add_player(QID, "Kitchen")
        with pytest.raises(MediaNotFoundError, match="No playable items found"):
            server.start_playing(QID, ["1"])
        server.add_qobuz_provider(available=False)
        with pytest.raises(MediaNotFoundError):
            server.start_playing(QID, ["1"])

    async def test_test_scheme_and_bad_uris(
        self, server: FakeMAServer, client: FakeMusicAssistantClient
    ) -> None:
        await client.player_queues.play_media(QID, "test://track/1", option=QueueOption.REPLACE)
        item = server.items(QID)[0]
        assert item.media_item is not None
        assert item.media_item.provider == "test"
        assert item.media_item.uri == "test://track/1"

        with pytest.raises(MediaNotFoundError):
            await client.player_queues.play_media(QID, "garbage", option=QueueOption.REPLACE)

    async def test_library_items_carry_the_qobuz_id_in_provider_mappings(
        self, server: FakeMAServer, client: FakeMusicAssistantClient
    ) -> None:
        server.library_track_ids.add("123")
        await client.player_queues.play_media(QID, qobuz("123"), option=QueueOption.REPLACE)
        await server.settle()

        queue = client.player_queues.get(QID)
        assert queue is not None and queue.current_item is not None
        media_item = queue.current_item.media_item
        assert media_item.uri.startswith("library://track/")
        assert [(m.provider_domain, m.item_id) for m in media_item.provider_mappings] == [
            ("qobuz", "123")
        ]

    async def test_duration_comes_from_the_catalogue(self, server: FakeMAServer) -> None:
        server.set_track("7", title="Song", artist="Band", duration=321)
        server.start_playing(QID, ["7", "8"])
        first, second = server.items(QID)
        assert first.duration == 321
        assert first.name == "Band - Song"
        assert second.duration == 200

    async def test_play_media_accepts_raw_send_command_with_extra_args(
        self, server: FakeMAServer, client: FakeMusicAssistantClient
    ) -> None:
        await client.send_command(
            "player_queues/play_media",
            queue_id=QID,
            media=[qobuz("1")],
            option=QueueOption.REPLACE,
            shuffle=False,
            some_future_argument=True,
        )
        assert server.track_ids(QID) == ["1"]
        assert server.calls("player_queues/play_media")[-1]["option"] == QueueOption.REPLACE


# ------------------------------------------------------------------- items and play_index


class TestItemsAndPlayIndex:
    async def test_delete_of_an_unbuffered_next_item_works(
        self, server: FakeMAServer, client: FakeMusicAssistantClient
    ) -> None:
        server.start_playing(QID, ["a", "b"])
        next_id = server.item_ids(QID)[1]

        await client.player_queues.delete_item(QID, next_id)

        assert server.track_ids(QID) == ["a"]

    async def test_delete_of_a_buffered_item_is_silently_ignored(
        self, server: FakeMAServer, client: FakeMusicAssistantClient
    ) -> None:
        server.start_playing(QID, ["a", "b"])
        server.buffer_next(QID)
        current_id, next_id = server.item_ids(QID)
        server.clear_logs()

        await client.player_queues.delete_item(QID, next_id)
        await client.player_queues.delete_item(QID, current_id)

        assert server.track_ids(QID) == ["a", "b"]
        assert server.events_of(EventType.QUEUE_ITEMS_UPDATED) == []

    async def test_delete_unknown_item(
        self, server: FakeMAServer, client: FakeMusicAssistantClient
    ) -> None:
        server.start_playing(QID, ["a"])
        with pytest.raises(InvalidDataError, match="not found in queue"):
            await client.player_queues.delete_item(QID, "nope")

    async def test_play_index_by_id_with_seek(
        self, server: FakeMAServer, client: FakeMusicAssistantClient
    ) -> None:
        server.start_playing(QID, ["a", "b", "c"])
        target = server.item_ids(QID)[2]

        await client.player_queues.play_index(QID, target, seek_position=30)

        queue = client.player_queues.get(QID)
        assert queue is not None and queue.current_item is not None
        assert queue.current_item.queue_item_id == target
        assert queue.current_index == 2
        assert queue.state == PlaybackState.PLAYING
        assert queue.corrected_elapsed_time == pytest.approx(30, abs=0.5)
        assert server.queue(QID).index_in_buffer == 2

    async def test_play_index_by_position_and_unknown_id(
        self, server: FakeMAServer, client: FakeMusicAssistantClient
    ) -> None:
        server.start_playing(QID, ["a", "b"])
        await client.player_queues.play_index(QID, 1)
        assert server.queue(QID).current_index == 1
        with pytest.raises(InvalidDataError):
            await client.player_queues.play_index(QID, "nope")

    async def test_clear_while_playing_stops(
        self, server: FakeMAServer, client: FakeMusicAssistantClient
    ) -> None:
        server.start_playing(QID, ["a", "b"])
        await client.player_queues.clear(QID)
        queue = server.queue(QID)
        assert server.track_ids(QID) == []
        assert queue.current_item is None
        assert queue.state == PlaybackState.IDLE
        assert server.players[QID].playback_state == PlaybackState.IDLE

    async def test_positioned_start_clear_add_play_index(
        self, server: FakeMAServer, client: FakeMusicAssistantClient
    ) -> None:
        """The backend's 'start at position' recipe: clear, ADD, play_index with seek."""
        server.start_playing(QID, ["old"])
        await client.player_queues.clear(QID)
        await client.player_queues.play_media(QID, qobuz("n"), option=QueueOption.ADD)
        queue = server.queue(QID)
        assert queue.state == PlaybackState.IDLE
        assert queue.current_index == 0

        await client.player_queues.play_index(QID, server.item_ids(QID)[0], seek_position=75)

        assert server.queue(QID).state == PlaybackState.PLAYING
        assert server.queue(QID).corrected_elapsed_time == pytest.approx(75, abs=0.5)


# ----------------------------------------------------------------------- playback flow


class TestPlaybackFlow:
    async def test_finish_moves_to_the_next_item(
        self, server: FakeMAServer, client: FakeMusicAssistantClient
    ) -> None:
        server.start_playing(QID, ["a", "b"])
        second = server.item_ids(QID)[1]
        server.advance_time(QID, 199)

        server.finish_current_track(QID)
        await server.settle()

        queue = client.player_queues.get(QID)
        assert queue is not None and queue.current_item is not None
        assert queue.current_item.queue_item_id == second
        assert queue.state == PlaybackState.PLAYING
        assert queue.corrected_elapsed_time == pytest.approx(0, abs=0.5)
        player = client.players.get(QID)
        assert player is not None and player.current_media is not None
        assert player.current_media.queue_item_id == second

    async def test_finish_plays_the_buffered_item_even_after_replace_next(
        self, server: FakeMAServer
    ) -> None:
        server.start_playing(QID, ["a", "b"])
        server.buffer_next(QID)
        server.start_playing(QID, ["n"], QueueOption.REPLACE_NEXT)

        server.finish_current_track(QID)

        queue = server.queue(QID)
        assert queue.current_item is not None
        assert queue.current_item.media_item.item_id == "b"

    async def test_end_of_queue_then_mark_ended(
        self, server: FakeMAServer, client: FakeMusicAssistantClient
    ) -> None:
        server.start_playing(QID, ["a", "b"])
        server.finish_current_track(QID)
        server.finish_current_track(QID)
        await server.settle()

        queue = client.player_queues.get(QID)
        assert queue is not None
        assert queue.state == PlaybackState.IDLE
        assert queue.ended is False

        assert server.mark_ended(QID) is True
        await server.settle()
        queue = client.player_queues.get(QID)
        assert queue is not None and queue.current_item is not None
        assert queue.ended is True
        assert queue.current_index == 1
        assert queue.current_item.media_item.item_id == "b"
        assert queue.elapsed_time == 0
        assert queue.index_in_buffer is None

    async def test_mark_ended_resumes_with_an_item_added_while_idle(
        self, server: FakeMAServer
    ) -> None:
        server.start_playing(QID, ["a"])
        server.finish_current_track(QID)
        server.start_playing(QID, ["late"], QueueOption.REPLACE_NEXT)

        assert server.mark_ended(QID) is False

        queue = server.queue(QID)
        assert queue.state == PlaybackState.PLAYING
        assert queue.current_item is not None
        assert queue.current_item.media_item.item_id == "late"
        assert queue.ended is False

    async def test_mark_ended_is_a_no_op_once_playing_again(self, server: FakeMAServer) -> None:
        server.start_playing(QID, ["a"])
        server.finish_current_track(QID)
        server.start_playing(QID, ["b"])
        assert server.mark_ended(QID) is False
        assert server.queue(QID).ended is False

    async def test_finish_requires_a_playing_queue(self, server: FakeMAServer) -> None:
        with pytest.raises(RuntimeError):
            server.finish_current_track(QID)

    async def test_previous_restarts_after_five_seconds(self, server: FakeMAServer) -> None:
        server.start_playing(QID, ["a", "b"])
        server.ui_next(QID)
        current = server.item_ids(QID)[1]
        server.advance_time(QID, 6)

        server.ui_previous(QID)

        queue = server.queue(QID)
        assert queue.current_item is not None
        assert queue.current_item.queue_item_id == current
        assert queue.corrected_elapsed_time == pytest.approx(0, abs=0.5)

    async def test_previous_goes_back_within_five_seconds(self, server: FakeMAServer) -> None:
        server.start_playing(QID, ["a", "b"])
        server.ui_next(QID)
        server.advance_time(QID, 3)

        server.ui_previous(QID)

        assert server.queue(QID).current_index == 0
        server.ui_previous(QID)  # already at the first item: restarts it
        assert server.queue(QID).current_index == 0
        assert server.queue(QID).state == PlaybackState.PLAYING

    async def test_next_is_a_no_op_at_the_end(self, server: FakeMAServer) -> None:
        server.start_playing(QID, ["a", "b"])
        server.ui_next(QID)
        assert server.queue(QID).current_index == 1
        last = server.item_ids(QID)[1]

        server.ui_next(QID)

        queue = server.queue(QID)
        assert queue.current_item is not None
        assert queue.current_item.queue_item_id == last
        assert queue.state == PlaybackState.PLAYING

    async def test_next_with_repeat_all_wraps(
        self, server: FakeMAServer, client: FakeMusicAssistantClient
    ) -> None:
        server.start_playing(QID, ["a", "b"])
        await client.player_queues.repeat(QID, RepeatMode.ALL)
        server.ui_next(QID)
        server.ui_next(QID)
        assert server.queue(QID).current_index == 0

    async def test_pause_resume_and_auto_stop(
        self, server: FakeMAServer, client: FakeMusicAssistantClient
    ) -> None:
        server.start_playing(QID, ["a"])
        current = server.item_ids(QID)[0]
        server.advance_time(QID, 42)

        server.ui_pause(QID)
        await server.settle()
        queue = client.player_queues.get(QID)
        assert queue is not None
        assert queue.state == PlaybackState.PAUSED
        assert queue.elapsed_time == pytest.approx(42, abs=0.5)
        assert server.queue(QID).resume_pos == 42

        assert server.auto_stop_after_pause(QID) is True
        await server.settle()
        queue = client.player_queues.get(QID)
        assert queue is not None and queue.current_item is not None
        assert queue.state == PlaybackState.IDLE
        assert queue.current_item.queue_item_id == current
        assert queue.resume_pos == 42
        assert server.auto_stop_after_pause(QID) is False

        server.ui_resume(QID)
        queue = server.queue(QID)
        assert queue.state == PlaybackState.PLAYING
        assert queue.current_item is not None
        assert queue.current_item.queue_item_id == current
        assert queue.corrected_elapsed_time == pytest.approx(42, abs=0.5)

    async def test_resume_from_pause_keeps_the_position(self, server: FakeMAServer) -> None:
        server.start_playing(QID, ["a"])
        server.advance_time(QID, 10)
        server.ui_pause(QID)
        server.ui_resume(QID)
        queue = server.queue(QID)
        assert queue.state == PlaybackState.PLAYING
        assert queue.corrected_elapsed_time == pytest.approx(10, abs=0.5)

    async def test_play_on_an_ended_queue_restarts_from_the_first_item(
        self, server: FakeMAServer, client: FakeMusicAssistantClient
    ) -> None:
        server.start_playing(QID, ["a", "b"])
        server.ui_next(QID)
        server.finish_current_track(QID)
        server.mark_ended(QID)

        await client.player_queues.play(QID)

        assert server.queue(QID).current_index == 0
        assert server.queue(QID).state == PlaybackState.PLAYING

    async def test_seek_emits_queue_time_updated(
        self, server: FakeMAServer, client: FakeMusicAssistantClient
    ) -> None:
        server.start_playing(QID, ["a"])
        await server.settle()
        events = record(client)

        server.ui_seek(QID, 120)
        await server.settle()

        time_events = [ev for ev in events if ev.event == EventType.QUEUE_TIME_UPDATED]
        assert len(time_events) == 1
        assert time_events[0].data == pytest.approx(120, abs=0.5)
        queue = client.player_queues.get(QID)
        assert queue is not None
        assert queue.corrected_elapsed_time == pytest.approx(120, abs=0.5)

    async def test_seek_while_paused_restarts_playback(self, server: FakeMAServer) -> None:
        """MA 2.10.7 seeks by restarting the stream (play_index): a paused queue plays again."""
        server.start_playing(QID, ["a"])
        server.ui_pause(QID)
        server.ui_seek(QID, 50)
        assert server.queue(QID).state == PlaybackState.PLAYING

    async def test_seek_validation(
        self, server: FakeMAServer, client: FakeMusicAssistantClient
    ) -> None:
        with pytest.raises(InvalidCommand, match="no item"):
            await client.player_queues.seek(QID, 10)
        server.start_playing(QID, ["a"])
        with pytest.raises(InvalidCommand, match="outside of duration"):
            await client.player_queues.seek(QID, 999)

    async def test_stream_failure_skips_to_the_next_item(self, server: FakeMAServer) -> None:
        server.stream_failure_track_ids.add("bad")
        server.start_playing(QID, ["bad", "good"])
        queue = server.queue(QID)
        assert queue.current_item is not None
        assert queue.current_item.media_item.item_id == "good"
        with pytest.raises(MediaNotFoundError, match="no more tracks"):
            server.start_playing(QID, ["bad"])
        assert server.queue(QID).state == PlaybackState.IDLE

    async def test_user_plays_foreign(
        self, server: FakeMAServer, client: FakeMusicAssistantClient
    ) -> None:
        server.start_playing(QID, ["a"])
        ours = set(server.item_ids(QID))

        server.user_plays_foreign(QID, "Jazz FM")
        await server.settle()

        queue = client.player_queues.get(QID)
        assert queue is not None and queue.current_item is not None
        assert queue.state == PlaybackState.PLAYING
        assert queue.current_item.queue_item_id not in ours
        assert queue.current_item.media_type == MediaType.RADIO
        assert queue.current_item.name == "Jazz FM"

        server.user_plays_foreign(QID, track_ids=["5"])
        item = server.items(QID)[0]
        assert item.media_item is not None
        assert item.media_item.uri == f"{QOBUZ_INSTANCE_ID}://track/5"

    async def test_switch_source(
        self, server: FakeMAServer, client: FakeMusicAssistantClient
    ) -> None:
        server.start_playing(QID, ["a"])
        await server.settle()
        events = record(client)

        server.switch_source(QID, "spotify")
        await server.settle()

        assert [ev.event for ev in events] == [EventType.PLAYER_UPDATED, EventType.QUEUE_UPDATED]
        queue = client.player_queues.get(QID)
        player = client.players.get(QID)
        assert queue is not None and player is not None
        assert queue.active is False
        assert queue.state == PlaybackState.IDLE
        assert player.active_source == "spotify"
        assert player.playback_state == PlaybackState.PLAYING
        assert player.current_media is not None
        assert player.current_media.queue_item_id is None

    async def test_advance_time_moves_cached_position_without_events(
        self, server: FakeMAServer, client: FakeMusicAssistantClient
    ) -> None:
        server.start_playing(QID, ["a"])
        await server.settle()
        events = record(client)

        server.advance_time(QID, 25)
        await server.settle()

        assert events == []
        queue = client.player_queues.get(QID)
        assert queue is not None
        assert queue.corrected_elapsed_time == pytest.approx(25, abs=0.5)
        assert server.queue(QID).corrected_elapsed_time == pytest.approx(25, abs=0.5)

    async def test_advance_time_also_shifts_frames_in_flight(
        self, server: FakeMAServer, client: FakeMusicAssistantClient
    ) -> None:
        server.start_playing(QID, ["a"])  # events queued, not yet read by the client
        server.advance_time(QID, 30)
        await server.settle()
        queue = client.player_queues.get(QID)
        assert queue is not None
        assert queue.corrected_elapsed_time == pytest.approx(30, abs=0.5)

    async def test_autoplay_fill_appends_foreign_tracks(self, server: FakeMAServer) -> None:
        server.start_playing(QID, ["a"])
        added = server.autoplay_fill(QID, 2)
        assert len(added) == 2
        assert server.track_ids(QID)[0] == "a"
        assert all(item.media_item.provider == "spotify--test" for item in added)


# --------------------------------------------------------------------------- players


class TestPlayers:
    async def test_volume_set(self, server: FakeMAServer, client: FakeMusicAssistantClient) -> None:
        events = record(client)
        await client.players.volume_set(QID, 55)
        player = client.players.get(QID)
        assert player is not None
        assert player.volume_level == 55
        assert [ev.event for ev in events] == [EventType.PLAYER_UPDATED]
        assert server.calls("players/cmd/volume_set") == [{"player_id": QID, "volume_level": 55}]

    async def test_group_volume_and_members(
        self, server: FakeMAServer, client: FakeMusicAssistantClient
    ) -> None:
        server.add_player("p2", "Bedroom", volume=10)
        server.add_player("grp", "Downstairs", type=PlayerType.GROUP, group_members=[QID, "p2"])
        await server.settle()
        group = client.players.get("grp")
        assert group is not None and group.group_volume == 20

        await client.players.group_volume("grp", 40)
        group = client.players.get("grp")
        member = client.players.get("p2")
        assert group is not None and member is not None
        assert group.group_volume == 40
        assert member.volume_level == 40

        server.set_player_volume(QID, 70)
        await server.settle()
        group = client.players.get("grp")
        assert group is not None and group.group_volume == 55

    async def test_unavailable_player_refuses_playback(
        self, server: FakeMAServer, client: FakeMusicAssistantClient
    ) -> None:
        server.update_player(QID, available=False)
        await server.settle()
        player = client.players.get(QID)
        assert player is not None and player.available is False
        with pytest.raises(PlayerUnavailableError):
            await client.player_queues.play_media(QID, qobuz("1"), option=QueueOption.REPLACE)

    async def test_add_and_remove_player_while_connected(
        self, server: FakeMAServer, client: FakeMusicAssistantClient
    ) -> None:
        server.add_player("p9", "Garage")
        await server.settle()
        assert client.players.get("p9") is not None
        assert client.player_queues.get("p9") is not None
        server.remove_player("p9")
        await server.settle()
        assert client.players.get("p9") is None

    async def test_player_commands_redirect_to_the_active_queue(
        self, server: FakeMAServer, client: FakeMusicAssistantClient
    ) -> None:
        server.start_playing(QID, ["a", "b"])
        await client.players.next_track(QID)
        assert server.queue(QID).current_index == 1
        await client.players.pause(QID)
        assert server.queue(QID).state == PlaybackState.PAUSED


# ------------------------------------------------------------------------ commands


class TestCommands:
    async def test_session_setup_commands(
        self, server: FakeMAServer, client: FakeMusicAssistantClient
    ) -> None:
        queue = server.queue(QID)
        queue.repeat_mode = RepeatMode.ONE
        queue.autoplay_enabled = True

        await client.player_queues.repeat(QID, RepeatMode.OFF)
        await client.send_command("player_queues/autoplay", queue_id=QID, autoplay_enabled=False)
        await server.settle()

        cached = client.player_queues.get(QID)
        assert cached is not None
        assert cached.repeat_mode == RepeatMode.OFF
        assert cached.autoplay_enabled is False

        await client.player_queues.dont_stop_the_music(QID, True)
        assert server.queue(QID).autoplay_enabled is True

    async def test_repeat_refused_on_dynamic_queue(
        self, server: FakeMAServer, client: FakeMusicAssistantClient
    ) -> None:
        server.queue(QID).is_dynamic = True
        with pytest.raises(InvalidCommand, match="dynamic"):
            await client.player_queues.repeat(QID, RepeatMode.ALL)

    async def test_unknown_command(
        self, server: FakeMAServer, client: FakeMusicAssistantClient
    ) -> None:
        with pytest.raises(InvalidCommand, match="Invalid command: player_queues/transfer"):
            await client.player_queues.transfer(QID, "other")

    async def test_fail_next_raises_the_real_error_class(
        self, server: FakeMAServer, client: FakeMusicAssistantClient
    ) -> None:
        server.fail_next["player_queues/play_media"] = MediaNotFoundError("Item 1 not found")

        with pytest.raises(MediaNotFoundError, match="Item 1 not found") as err:
            await client.player_queues.play_media(QID, qobuz("1"), option=QueueOption.REPLACE)

        assert type(err.value) is MediaNotFoundError
        assert server.track_ids(QID) == []
        assert "player_queues/play_media" not in server.fail_next
        await client.player_queues.play_media(QID, qobuz("1"), option=QueueOption.REPLACE)
        assert server.track_ids(QID) == ["1"]

    async def test_get_queue_items_and_reads(
        self, server: FakeMAServer, client: FakeMusicAssistantClient
    ) -> None:
        server.start_playing(QID, ["a", "b", "c"])
        items = await client.player_queues.get_queue_items(QID, limit=2, offset=1)
        assert [item.media_item.item_id for item in items] == ["b", "c"]
        active = await client.player_queues.get_active_queue(QID)
        assert active is not None and active.queue_id == QID
        track = await client.music.get_item_by_uri(qobuz("b"))
        assert track.uri == f"{QOBUZ_INSTANCE_ID}://track/b"
        track = await client.music.get_track("b", "qobuz")
        assert track.item_id == "b"

    async def test_writes_excludes_reads_and_auth(
        self, server: FakeMAServer, client: FakeMusicAssistantClient
    ) -> None:
        names = [name for name, _ in server.command_log]
        assert "players/all" in names and "player_queues/all" in names
        assert "auth" not in names
        assert server.writes == []
        await client.player_queues.pause(QID)
        assert server.writes == [("player_queues/pause", {"queue_id": QID})]

    async def test_other_commands_do_not_crash(
        self, server: FakeMAServer, client: FakeMusicAssistantClient
    ) -> None:
        server.start_playing(QID, ["a", "b", "c", "d"])
        ids = server.item_ids(QID)
        await client.player_queues.move_item(QID, ids[3], -1)
        assert server.track_ids(QID) == ["a", "b", "d", "c"]
        with pytest.raises(Exception, match="already played/buffered"):
            await client.player_queues.move_item(QID, ids[0], 1)
        await client.player_queues.skip(QID, 20)
        assert server.queue(QID).corrected_elapsed_time == pytest.approx(20, abs=0.5)
        await client.player_queues.shuffle(QID, True)
        assert server.queue(QID).shuffle_enabled is True
        assert server.track_ids(QID)[0] == "a"
        await client.player_queues.shuffle(QID, False)
        await client.send_command("player_queues/crossfade", queue_id=QID, crossfade_enabled=True)
        assert server.queue(QID).crossfade_enabled is True
        await client.player_queues.play_pause(QID)
        assert server.queue(QID).state == PlaybackState.PAUSED
        await client.player_queues.play_pause(QID)
        assert server.queue(QID).state == PlaybackState.PLAYING
        await client.player_queues.resume(QID)
        await client.player_queues.next(QID)
        await client.player_queues.previous(QID)

        await client.players.volume_up(QID)
        await client.players.volume_down(QID)
        await client.players.volume_down(QID)
        assert server.players[QID].volume_level == 25
        await client.players.volume_mute(QID, True)
        assert server.players[QID].volume_muted is True
        await client.players.seek(QID, 30)
        await client.players.play_pause(QID)
        await client.players.play(QID)
        await client.players.stop(QID)
        assert server.queue(QID).state == PlaybackState.IDLE
        await client.players.power(QID, False)
        assert server.players[QID].powered is False

        server.add_player("p2", "Bedroom")
        await client.players.set_members(QID, player_ids_to_add=["p2"])
        assert list(server.players[QID].group_members) == ["p2"]
        await client.players.select_source(QID, "linein")
        await server.settle()
        cached_player = client.players.get(QID)
        assert cached_player is not None and cached_player.active_source == "linein"
        # MA only signals the queue when its compare state changes: an idle queue going
        # inactive sends no QUEUE_UPDATED, so the cached queue still says active
        cached_queue = client.player_queues.get(QID)
        assert cached_queue is not None and cached_queue.active is True
        assert server.queue(QID).active is False
        player = await client.send_command("players/get", player_id=QID)
        assert player["active_source"] == "linein"
        item = await client.music.get_item(MediaType.TRACK, "9", "qobuz")
        assert item.item_id == "9"
        with pytest.raises(PlayerUnavailableError):
            await client.player_queues.play("nope")
        with pytest.raises(PlayerUnavailableError):
            await client.players.volume_set("nope", 3)


# -------------------------------------------------------------------------- events


class TestEvents:
    async def test_replace_event_sequence(
        self, server: FakeMAServer, client: FakeMusicAssistantClient
    ) -> None:
        events = record(client)

        await client.player_queues.play_media(QID, qobuz("1"), option=QueueOption.REPLACE)

        # every event caused by the command was delivered before the command returned
        assert [ev.event for ev in events] == [
            EventType.QUEUE_UPDATED,  # play action starts (flag on)
            EventType.QUEUE_ITEMS_UPDATED,
            EventType.QUEUE_UPDATED,  # new items, old position
            EventType.QUEUE_UPDATED,  # play_index: new current item, not playing yet
            EventType.QUEUE_UPDATED,  # player reports PLAYING (flag already off)
            EventType.QUEUE_UPDATED,  # play action ends
            EventType.PLAYER_UPDATED,
        ]
        flags = [
            ev.data["extra_attributes"][ATTR_PLAY_ACTION_IN_PROGRESS]
            for ev in events
            if ev.event == EventType.QUEUE_UPDATED
        ]
        assert flags == [True, True, True, False, False]
        playing = [
            ev
            for ev in events
            if ev.event == EventType.QUEUE_UPDATED and ev.data["state"] == "playing"
        ]
        assert playing and not playing[0].data["extra_attributes"][ATTR_PLAY_ACTION_IN_PROGRESS]

    async def test_failed_play_media_still_flaps_the_flag(
        self, server: FakeMAServer, client: FakeMusicAssistantClient
    ) -> None:
        server.unplayable_track_ids.add("x")
        events = record(client)
        with pytest.raises(MediaNotFoundError):
            await client.player_queues.play_media(QID, qobuz("x"), option=QueueOption.REPLACE)
        assert [ev.event for ev in events] == [EventType.QUEUE_UPDATED, EventType.QUEUE_UPDATED]

    async def test_cache_is_updated_before_subscribers_run(
        self, server: FakeMAServer, client: FakeMusicAssistantClient
    ) -> None:
        seen: list[tuple[Any, Any]] = []

        def on_event(event: MassEvent) -> None:
            if event.event == EventType.QUEUE_UPDATED:
                cached = client.player_queues.get(QID)
                assert cached is not None
                seen.append((event.data["state"], cached.state.value))
            elif event.event == EventType.PLAYER_UPDATED:
                cached_player = client.players.get(QID)
                assert cached_player is not None
                seen.append((event.data["playback_state"], cached_player.playback_state.value))

        client.subscribe(on_event)
        server.start_playing(QID, ["a", "b"])
        server.ui_pause(QID)
        server.ui_resume(QID)
        server.finish_current_track(QID)
        await server.settle()

        assert seen
        assert all(event_state == cached_state for event_state, cached_state in seen)

    async def test_event_data_is_a_plain_dict(
        self, server: FakeMAServer, client: FakeMusicAssistantClient
    ) -> None:
        events = record(client)
        server.start_playing(QID, ["a"])
        server.ui_seek(QID, 60)
        await server.settle()
        for ev in events:
            if ev.event == EventType.QUEUE_TIME_UPDATED:
                assert isinstance(ev.data, float)
            else:
                assert isinstance(ev.data, dict), ev.event
        assert {ev.event for ev in events} >= {
            EventType.QUEUE_UPDATED,
            EventType.QUEUE_ITEMS_UPDATED,
            EventType.QUEUE_TIME_UPDATED,
            EventType.PLAYER_UPDATED,
        }

    async def test_filters_async_callbacks_and_errors(
        self,
        server: FakeMAServer,
        client: FakeMusicAssistantClient,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        received: list[EventType] = []

        async def on_player(event: MassEvent) -> None:
            received.append(event.event)

        def broken(event: MassEvent) -> None:
            raise ValueError("boom")

        client.subscribe(on_player, EventType.PLAYER_UPDATED, QID)
        unsubscribe = client.subscribe(broken)
        with caplog.at_level(logging.ERROR):
            server.set_player_volume(QID, 12)
            await server.settle()
        assert received == [EventType.PLAYER_UPDATED]
        assert "boom" in caplog.text

        unsubscribe()
        with pytest.raises(ValueError):
            unsubscribe()

    async def test_player_updates_after_result(
        self, server: FakeMAServer, client: FakeMusicAssistantClient
    ) -> None:
        server.player_updates_after_result = True

        await client.player_queues.play_media(QID, qobuz("1"), option=QueueOption.REPLACE)

        player = client.players.get(QID)
        assert player is not None and player.playback_state == PlaybackState.IDLE
        await server.settle()
        player = client.players.get(QID)
        assert player is not None and player.playback_state == PlaybackState.PLAYING


# ---------------------------------------------------------------- client lifecycle


class TestClientLifecycle:
    async def test_connect_errors(self, server: FakeMAServer) -> None:
        server.reachable = False
        with pytest.raises(CannotConnect):
            await server.client_factory(server.url, None, server.token).connect()
        server.reachable = True

        with pytest.raises(CannotConnect):
            await server.client_factory("http://10.0.0.9:8095", None, server.token).connect()

        with pytest.raises(AuthenticationRequired):
            await server.client_factory(server.url, None, None).connect()

        with pytest.raises(AuthenticationFailed) as err:
            await server.client_factory(server.url, None, "wrong").connect()
        assert str(err.value) == "Authentication failed: Invalid or expired token"
        assert isinstance(err.value.__cause__, InvalidToken)

        server.min_schema_version = 47
        with pytest.raises(InvalidServerVersion):
            await server.client_factory(server.url, None, server.token).connect()
        server.min_schema_version = 28

        server.setup_required = True
        with pytest.raises(AuthenticationFailed, match="Connection was closed"):
            await server.client_factory(server.url, None, server.token).connect()

        assert server.clients == []

    async def test_client_is_single_use(self, server: FakeMAServer) -> None:
        client, task = await server.connect_client()
        events = record(client)
        await client.disconnect()
        assert await asyncio.wait_for(task, 1) is None

        with pytest.raises(InvalidState, match="Not connected"):
            await client.player_queues.play(QID)

        server.start_playing(QID, ["a"])
        await server.settle()
        assert events == []

        ready = asyncio.Event()
        await asyncio.wait_for(client.start_listening(ready), 1)
        assert not ready.is_set()
        assert server.clients == []

    async def test_graceful_drop_returns_none(self, server: FakeMAServer) -> None:
        client, task = await server.connect_client()
        assert server.clients == [client]
        server.drop_connections(graceful=True)
        assert await asyncio.wait_for(task, 1) is None
        assert not client.connection.connected
        assert server.clients == []

    async def test_hard_drop_raises_connection_failed(self, server: FakeMAServer) -> None:
        _client, task = await server.connect_client()
        server.drop_connections(graceful=False)
        with pytest.raises(ConnectionFailed):
            await asyncio.wait_for(task, 1)

    async def test_pending_command_is_cancelled_by_a_drop(self, server: FakeMAServer) -> None:
        client, task = await server.connect_client()
        gate = server.gate("player_queues/play_media")
        pending = asyncio.create_task(
            client.player_queues.play_media(QID, qobuz("1"), option=QueueOption.REPLACE)
        )
        await server.settle()
        assert not pending.done()

        server.drop_connections()
        assert await asyncio.wait_for(task, 1) is None
        with pytest.raises(asyncio.CancelledError):
            await pending
        gate.set()  # the server still runs a command it had received
        await server.settle()
        assert server.track_ids(QID) == ["1"]

    async def test_fail_next_drop(self, server: FakeMAServer) -> None:
        client, task = await server.connect_client()
        server.fail_next["player_queues/stop"] = DROP
        with pytest.raises(asyncio.CancelledError):
            await client.player_queues.stop(QID)
        assert await asyncio.wait_for(task, 1) is None

    async def test_direct_read_mode_and_concurrent_receive(self, server: FakeMAServer) -> None:
        async with server.client_class(server.url, None, server.token) as client:
            user = await client.auth.get_current_user()
            assert user.username == "admin"
            results = await asyncio.gather(
                client.auth.get_current_user(), client.auth.get_tokens(), return_exceptions=True
            )
            assert any(isinstance(r, RuntimeError) for r in results)
        assert not client.connection.connected

    async def test_restart_keeps_items_and_ids(self, server: FakeMAServer) -> None:
        client, task = await server.connect_client()
        events = record(client)
        server.start_playing(QID, ["a", "b"])
        server.advance_time(QID, 33)
        await server.settle()
        ids = server.item_ids(QID)

        server.restart()
        assert await asyncio.wait_for(task, 1) is None
        unavailable = [
            ev for ev in events if ev.event == EventType.PLAYER_UPDATED and not ev.data["available"]
        ]
        assert unavailable

        with pytest.raises(CannotConnect):
            await server.client_factory(server.url, None, server.token).connect()

        server.come_back()
        client2, task2 = await server.connect_client()
        assert client2 is not client
        queue = client2.player_queues.get(QID)
        assert queue is not None and queue.current_item is not None
        assert queue.state == PlaybackState.IDLE
        assert server.item_ids(QID) == ids
        assert queue.current_item.queue_item_id == ids[0]
        assert queue.resume_pos == 33
        player = client2.players.get(QID)
        assert player is not None and player.available is True

        await client2.player_queues.play(QID)  # resumes where MA stopped
        assert server.queue(QID).corrected_elapsed_time == pytest.approx(33, abs=0.5)
        await client2.disconnect()
        await asyncio.wait_for(task2, 1)

    async def test_revoked_token(self, server: FakeMAServer) -> None:
        _client, task = await server.connect_client()
        server.revoke_token(server.token)
        assert await asyncio.wait_for(task, 1) is None

        with pytest.raises(AuthenticationFailed) as err:
            await server.client_factory(server.url, None, server.token).connect()
        assert isinstance(err.value.__cause__, InvalidToken)

    async def test_hang_init_leaves_init_ready_unset(self, server: FakeMAServer) -> None:
        server.hang_init = True
        client = server.client_factory(server.url, None, server.token)
        ready = asyncio.Event()
        task = asyncio.create_task(client.start_listening(ready))
        await server.settle()
        assert not ready.is_set()
        assert client.players.get(QID) is not None
        await client.disconnect()
        await asyncio.wait_for(task, 1)

    async def test_state_outlives_clients(self, server: FakeMAServer) -> None:
        client, task = await server.connect_client()
        await client.player_queues.play_media(QID, qobuz("1"), option=QueueOption.REPLACE)
        server.drop_connections()
        await asyncio.wait_for(task, 1)

        client2, task2 = await server.connect_client()
        queue = client2.player_queues.get(QID)
        assert queue is not None and queue.state == PlaybackState.PLAYING
        assert len(server.created_clients) == 2
        await client2.disconnect()
        await asyncio.wait_for(task2, 1)

    async def test_bound_class_can_replace_music_assistant_client(
        self, server: FakeMAServer
    ) -> None:
        with pytest.raises(TypeError):
            FakeMusicAssistantClient(server.url, None, server.token)
        client = server.client_class(server.url, None, token=server.token, locale="de")
        await client.connect()
        assert client.server_info is not None
        assert client.server_info.server_version == "2.10.7"
        assert client.server_info.schema_version == 65
        await client.disconnect()

    async def test_logout_closes_the_socket(self, server: FakeMAServer) -> None:
        client, task = await server.connect_client()
        await client.auth.logout()
        assert await asyncio.wait_for(task, 1) is None
        assert server.token not in server.valid_tokens

    async def test_send_after_server_close_is_cancelled(self, server: FakeMAServer) -> None:
        """A command written to a socket the server already closed never gets an answer."""
        client, task = await server.connect_client()
        server.drop_connections()
        assert client.connection.connected  # the client has not read the close frame yet
        with pytest.raises(asyncio.CancelledError):
            await client.player_queues.pause(QID)
        await asyncio.wait_for(task, 1)
        # once the close frame was read, commands fail fast instead
        with pytest.raises(InvalidState):
            await client.player_queues.pause(QID)

    async def test_receive_after_close_keeps_raising(self, server: FakeMAServer) -> None:
        client = server.client_factory(server.url, None, server.token)
        await client.connect()
        server.drop_connections()
        for _ in range(2):
            with pytest.raises(ConnectionClosed):
                await client.connection.receive_message()
        await client.disconnect()


# --------------------------------------------------------------------------- login


class TestLoginHelpers:
    async def test_login_with_token(self, server: FakeMAServer) -> None:
        user, token = await server.login_with_token(
            server.url, "admin", "admin-password", token_name="Qobuz Bridge"
        )
        assert user.username == "admin"
        assert token in server.valid_tokens
        assert server.calls("auth/token/create") == [{"name": "Qobuz Bridge", "user_id": None}]
        client, task = await server.connect_client(token=token)
        await client.disconnect()
        await asyncio.wait_for(task, 1)

    async def test_login_errors(self, server: FakeMAServer) -> None:
        with pytest.raises(LoginFailed, match="Invalid username or password"):
            await server.login_with_token(server.url, "admin", "nope")

        server.users["guest"] = "guest-password"
        server.guest_users.add("guest")
        with pytest.raises(InsufficientPermissions):
            await server.login_with_token(server.url, "guest", "guest-password")

        server.setup_required = True
        with pytest.raises(CannotConnect, match="403"):
            await server.login(server.url, "admin", "admin-password")
        server.setup_required = False

        server.reachable = False
        with pytest.raises(CannotConnect):
            await server.login(server.url, "admin", "admin-password")
        with pytest.raises(CannotConnect):
            await server.get_server_info(server.url)

    async def test_get_server_info(self, server: FakeMAServer) -> None:
        info = await server.get_server_info(server.url)
        assert info.server_version == "2.10.7"
        assert info.min_supported_schema_version == 28

    async def test_tokens_listing_and_revoke(self, server: FakeMAServer) -> None:
        _user, token = await server.login_with_token(server.url, "admin", "admin-password")
        client, task = await server.connect_client(token=token)
        tokens = await client.auth.get_tokens()
        mine = [t for t in tokens if t.is_long_lived]
        assert len(mine) >= 2
        await client.auth.revoke_token(mine[-1].token_id)
        assert await asyncio.wait_for(task, 1) is None
        assert token not in server.valid_tokens
