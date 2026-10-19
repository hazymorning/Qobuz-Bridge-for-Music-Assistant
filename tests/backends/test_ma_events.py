"""How MusicAssistantBackend maps Music Assistant events onto the Player callbacks.

Natural ends, gapless transitions, MA-UI actions, takeovers by other sources, lost
connections and add-on restarts. Uses the rig from ``conftest.py`` (fake MA server +
real client + real connection, Player callbacks mocked).
"""

import asyncio
import json
from typing import Any, Callable

import pytest

pytest.importorskip("music_assistant_models")
pytest.importorskip("music_assistant_client")

from music_assistant_models.enums import PlaybackState as MAState  # noqa: E402

from qobuz_proxy.backends.music_assistant.backend import MusicAssistantBackend  # noqa: E402
from qobuz_proxy.backends.music_assistant.connection import ConnectionState  # noqa: E402
from qobuz_proxy.backends.types import PlaybackState  # noqa: E402
from tests.backends.conftest import (  # noqa: E402
    NOTHING_FIRED,
    QID,
    STREAM_URL,
    Callbacks,
    Rig,
    meta,
)


async def other_client_clears_the_queue(r: Rig) -> None:
    """Someone presses "Clear queue" in MA's UI."""
    other, listen = await r.server.connect_client()
    await other.player_queues.clear(QID)
    await other.disconnect()
    await listen
    await r.settle()


# --------------------------------------------------------------------------- track end


class TestTrackEnd:
    async def test_gapless_transition_reports_next_track_started(self, rig: Rig) -> None:
        await rig.play_for("101", 195)
        assert await rig.arm("102")
        rig.cb.position.reset_mock()

        rig.server.finish_current_track(QID)

        await rig.wait(lambda: rig.cb.next_started.called)
        await rig.idle(0.2)
        assert rig.cb.endings() == {**NOTHING_FIRED, "next_started": 1}
        rig.cb.position.assert_called_with(0)
        assert rig.backend._current is not None and rig.backend._current.track_id == "102"
        assert await rig.backend.get_state() == PlaybackState.PLAYING
        assert await rig.backend.get_position() == pytest.approx(0, abs=1_000)

    @pytest.mark.parametrize("mark_ended", ["never", "before_settle", "after_settle"])
    async def test_natural_end_reports_track_ended_once_after_the_idle_settles(
        self,
        rig: Rig,
        timing: Callable[..., None],
        mark_ended: str,
    ) -> None:
        timing(IDLE_SETTLE_SECONDS=0.2)
        await rig.play_for("101", 197)

        rig.server.finish_current_track(QID)
        await rig.settle()
        if mark_ended == "before_settle":
            assert rig.server.mark_ended(QID)  # MA's end-of-queue settle
            await rig.settle()
        assert rig.cb.track_ended.call_count == 0  # not decided yet
        assert await rig.backend.get_state() == PlaybackState.PLAYING

        await rig.wait(lambda: rig.cb.track_ended.called, what="track_ended")
        if mark_ended == "after_settle":
            assert rig.server.mark_ended(QID)
        await rig.idle(0.3)

        assert rig.cb.endings() == {**NOTHING_FIRED, "track_ended": 1}
        assert await rig.backend.get_state() == PlaybackState.STOPPED

    async def test_short_idle_flap_is_ignored(
        self,
        rig: Rig,
        timing: Callable[..., None],
    ) -> None:
        timing(IDLE_SETTLE_SECONDS=0.2)
        await rig.play_for("101", 30)

        rig.server.device_reports(QID, MAState.IDLE)  # stream restart for a seek...
        await rig.settle()
        assert rig.queue().state == MAState.IDLE
        rig.server.device_reports(QID, MAState.PLAYING)  # ...and it plays again
        await rig.idle(0.3)

        assert rig.cb.endings() == NOTHING_FIRED
        assert await rig.backend.get_state() == PlaybackState.PLAYING

    async def test_idle_flap_after_our_own_seek_is_ignored(
        self,
        rig: Rig,
        timing: Callable[..., None],
    ) -> None:
        timing(IDLE_SETTLE_SECONDS=0.2)
        await rig.play_for("101", 30)
        await rig.backend.seek(120_000)
        await rig.settle()

        rig.server.device_reports(QID, MAState.IDLE)
        await rig.settle()
        rig.server.device_reports(QID, MAState.PLAYING)
        await rig.idle(0.3)

        assert rig.cb.endings() == NOTHING_FIRED
        assert rig.cb.position.call_count == 0  # our own seek is not echoed as a jump

    async def test_speaker_that_never_starts_is_interrupted(
        self,
        rig: Rig,
        timing: Callable[..., None],
    ) -> None:
        timing(START_GRACE_SECONDS=0.1)
        rig.server.dead_player_ids.add(QID)

        await rig.play("101")
        assert rig.queue().state == MAState.IDLE
        assert await rig.backend.get_state() == PlaybackState.PLAYING  # still starting

        await rig.wait(
            lambda: rig.cb.interrupted.called or rig.cb.track_ended.called, what="a decision"
        )
        await rig.idle(0.2)
        # Advancing would skip through the whole album on a dead speaker
        assert rig.cb.endings() == {**NOTHING_FIRED, "interrupted": 1}

    async def test_stall_watchdog_interrupts_once(self, rig: Rig) -> None:
        await rig.play_for("101", 30)

        rig.server.advance_time(QID, 200)  # MA still says "playing" 30 s past the end
        await rig.poll()

        await rig.wait(lambda: rig.cb.interrupted.called, what="stall detected")
        for _ in range(3):  # the Player keeps polling
            await rig.poll()
        await rig.idle(0.2)
        assert rig.cb.endings() == {**NOTHING_FIRED, "interrupted": 1}
        assert await rig.backend.get_state() == PlaybackState.STOPPED

    async def test_no_stall_while_inside_the_track(self, rig: Rig) -> None:
        await rig.play_for("101", 205)  # 5 s past MA's duration is within the margin
        await rig.poll()
        assert rig.cb.endings() == NOTHING_FIRED


# --------------------------------------------------------------------- MA UI actions


class TestMAUserActions:
    async def test_stop_in_ma_mid_track_is_an_interruption(self, rig: Rig) -> None:
        await rig.play_for("101", 60)

        rig.server.ui_stop(QID)

        await rig.wait(lambda: rig.cb.interrupted.called)
        assert rig.cb.interrupted.call_args.args[0] == pytest.approx(60_000, abs=1_500)
        rig.server.mark_ended(QID)
        await rig.idle(0.3)
        assert rig.cb.endings() == {**NOTHING_FIRED, "interrupted": 1}
        assert await rig.backend.get_state() == PlaybackState.STOPPED

    async def test_stop_in_ma_in_the_last_seconds_is_still_an_interruption(self, rig: Rig) -> None:
        await rig.play_for("101", 195)  # 5 s before the end

        rig.server.ui_stop(QID)

        await rig.wait(lambda: rig.cb.interrupted.called or rig.cb.track_ended.called)
        await rig.idle(0.3)
        assert rig.cb.endings() == {**NOTHING_FIRED, "interrupted": 1}

    async def test_stop_in_ma_right_after_the_start_is_an_interruption(
        self,
        rig: Rig,
        timing: Callable[..., None],
    ) -> None:
        timing(START_GRACE_SECONDS=0.3)
        await rig.play("101")
        rig.server.advance_time(QID, 1)
        await rig.poll()

        rig.server.ui_stop(QID)  # inside the start grace

        await rig.wait(lambda: rig.cb.interrupted.called or rig.cb.track_ended.called, timeout=3.0)
        await rig.idle(0.2)
        assert rig.cb.endings() == {**NOTHING_FIRED, "interrupted": 1}

    async def test_pause_resume_and_auto_stop_in_ma(self, rig: Rig) -> None:
        await rig.play_for("101", 30)

        rig.server.ui_pause(QID)
        await rig.settle()
        assert await rig.backend.get_state() == PlaybackState.PAUSED

        assert rig.server.auto_stop_after_pause(QID)  # 30 s later MA stops the queue
        await rig.idle(0.2)
        assert rig.ma_state() == MAState.IDLE
        assert await rig.backend.get_state() == PlaybackState.PAUSED

        rig.server.ui_resume(QID)
        await rig.settle()
        assert await rig.backend.get_state() == PlaybackState.PLAYING
        assert rig.queue().corrected_elapsed_time == pytest.approx(30, abs=1)
        assert rig.cb.endings() == NOTHING_FIRED

    async def test_next_in_ma_moves_to_the_armed_track(self, rig: Rig) -> None:
        await rig.play_for("101", 30)
        assert await rig.arm("102")
        rig.cb.position.reset_mock()

        rig.server.ui_next(QID)

        await rig.wait(lambda: rig.cb.next_started.called)
        await rig.idle(0.2)
        assert rig.cb.endings() == {**NOTHING_FIRED, "next_started": 1}
        rig.cb.position.assert_called_with(0)
        assert rig.ma_track() == "102"

    async def test_previous_in_ma_to_an_earlier_track_keeps_the_session(self, rig: Rig) -> None:
        await rig.play_for("101", 195)
        assert await rig.arm("102")
        rig.server.finish_current_track(QID)
        await rig.wait(lambda: rig.cb.next_started.called)
        await rig.idle(0.06)

        rig.server.ui_previous(QID)  # within 5 s of 102's start: back to 101

        await rig.wait(lambda: rig.cb.interrupted.called)
        await rig.idle(0.2)
        assert rig.ma_track() == "101"
        assert rig.cb.endings() == {**NOTHING_FIRED, "next_started": 1, "interrupted": 1}
        assert rig.backend._external is False
        assert await rig.backend.can_apply_remote_state() is True

    async def test_clearing_the_queue_in_ma_is_an_interruption(self, rig: Rig) -> None:
        await rig.play_for("101", 30)

        await other_client_clears_the_queue(rig)

        await rig.wait(lambda: rig.cb.interrupted.called)
        await rig.idle(0.2)
        assert rig.cb.endings() == {**NOTHING_FIRED, "interrupted": 1}
        assert rig.backend._external is False

    async def test_seek_in_ma_updates_the_position(self, rig: Rig) -> None:
        await rig.play_for("101", 10)
        rig.cb.position.reset_mock()

        rig.server.ui_seek(QID, 120)

        await rig.wait(lambda: rig.cb.position.called)
        assert rig.cb.position.call_args.args[0] == pytest.approx(120_000, abs=1_500)
        await rig.idle(0.2)
        assert rig.cb.endings() == NOTHING_FIRED
        assert await rig.backend.get_position() == pytest.approx(120_000, abs=1_500)

    async def test_position_jump_reported_by_the_speaker_updates_the_position(
        self, rig: Rig
    ) -> None:
        await rig.play_for("101", 10)
        rig.cb.position.reset_mock()

        # The speaker jumps by itself (its own app/buttons): MA only sends QUEUE_TIME_UPDATED
        rig.server.device_reports(QID, elapsed=90.0)

        await rig.wait(lambda: rig.cb.position.called)
        assert rig.cb.position.call_args.args[0] == pytest.approx(90_000, abs=1_500)
        assert rig.cb.endings() == NOTHING_FIRED


# --------------------------------------------------------------------------- takeover


class TestTakeover:
    async def test_foreign_content_releases_the_session_and_stops_writing(self, rig: Rig) -> None:
        await rig.play_for("101", 30)
        assert await rig.arm("102")

        rig.server.user_plays_foreign(QID, "Radio 1")

        await rig.wait(lambda: rig.cb.external.await_count == 1, what="takeover")
        assert await rig.backend.check_external_playback() is True
        assert await rig.backend.can_apply_remote_state() is False
        assert await rig.backend.get_state() == PlaybackState.STOPPED
        writes = list(rig.server.writes)

        # More MA activity, then whatever the Qobuz app or a replayed snapshot sends
        rig.server.ui_pause(QID)
        rig.server.ui_resume(QID)
        rig.server.user_plays_foreign(QID, "Radio 2")
        rig.server.set_player_volume(QID, 70)
        await rig.idle(0.1)
        with pytest.raises(RuntimeError):
            await rig.backend.play(STREAM_URL, meta("103"))
        await rig.backend.set_volume(20)
        await rig.backend.stop()
        await rig.backend.stop(next_track_id="102")
        assert await rig.backend.resume() is False
        assert await rig.backend.set_next_track(STREAM_URL, meta("104")) is False
        await rig.backend.clear_next_track()
        with pytest.raises(RuntimeError):
            await rig.backend.seek(10_000)
        await rig.idle(0.2)

        assert rig.server.writes == writes
        assert rig.cb.external.await_count == 1
        assert rig.server.queue(QID).current_item.name == "Radio 2"

    async def test_reselection_lets_qobuz_play_again(self, rig: Rig) -> None:
        await rig.play_for("101", 30)
        rig.server.user_plays_foreign(QID, "Radio 1")
        await rig.wait(lambda: rig.cb.external.await_count == 1)

        rig.backend.prepare_for_selection()  # the user picks the device in the Qobuz app

        assert await rig.backend.check_external_playback() is False
        assert await rig.backend.can_apply_remote_state() is True  # MA still plays the radio
        await rig.play("105")
        assert rig.ma_track() == "105"
        assert await rig.backend.get_state() == PlaybackState.PLAYING
        await rig.idle(0.2)
        assert rig.cb.external.await_count == 1

    async def test_switching_the_speakers_source_is_a_takeover(self, rig: Rig) -> None:
        await rig.play_for("101", 30)

        rig.server.switch_source(QID, "spotify")

        await rig.wait(lambda: rig.cb.external.await_count == 1)
        assert rig.cb.interrupted.call_count == 0 and rig.cb.track_ended.call_count == 0

    async def test_qobuz_played_from_mas_own_ui_is_a_takeover(self, rig: Rig) -> None:
        await rig.play_for("101", 30)

        rig.server.user_plays_foreign(QID, track_ids=["555"])

        await rig.wait(lambda: rig.cb.external.await_count == 1)

    async def test_foreign_content_after_our_stop_is_just_forgotten(self, rig: Rig) -> None:
        await rig.play_for("101", 30)
        await rig.backend.stop()

        rig.server.user_plays_foreign(QID, "Radio 1")
        await rig.idle(0.2)

        assert rig.cb.endings() == NOTHING_FIRED
        assert rig.backend._external is False
        assert rig.backend._current is None
        assert await rig.backend.can_apply_remote_state() is True

    async def test_check_external_playback_waits_for_the_release(self, rig: Rig) -> None:
        released: list[str] = []

        async def release() -> None:
            await asyncio.sleep(0.05)
            released.append("released")

        rig.backend.on_external_playback(release)
        await rig.play_for("101", 30)
        rig.server.user_plays_foreign(QID, "Radio 1")
        await rig.settle()
        assert released == []  # the release is still running

        assert await rig.backend.check_external_playback() is True

        assert released == ["released"]

    async def test_a_fresh_selection_may_take_a_busy_player(self, rig: Rig) -> None:
        rig.server.user_plays_foreign(QID, "Radio 1")  # MA was already playing radio
        await rig.settle()

        rig.backend.prepare_for_selection()

        assert await rig.backend.check_external_playback() is False
        assert await rig.backend.can_apply_remote_state() is True
        await rig.play("101")
        assert rig.ma_track() == "101"
        await rig.idle(0.2)
        assert rig.cb.endings() == NOTHING_FIRED


# ------------------------------------------------------------------- connection loss


class TestConnectionLoss:
    async def test_disconnect_while_playing_interrupts_after_the_grace(
        self,
        rig: Rig,
        timing: Callable[..., None],
    ) -> None:
        timing(DISCONNECT_GRACE_SECONDS=0.3)
        await rig.play_for("101", 30)

        await rig.lose_ma()
        assert rig.cb.interrupted.call_count == 0  # within the grace
        assert await rig.backend.get_state() == PlaybackState.PLAYING  # frozen

        await rig.wait(lambda: rig.cb.interrupted.called, what="interrupted")
        await rig.idle(0.4)  # more reconnect attempts fail meanwhile
        assert rig.cb.endings() == {**NOTHING_FIRED, "interrupted": 1}
        assert rig.cb.interrupted.call_args.args[0] == pytest.approx(30_000, abs=1_500)
        assert await rig.backend.get_state() == PlaybackState.STOPPED

    async def test_reconnect_within_the_grace_changes_nothing(
        self,
        rig: Rig,
        timing: Callable[..., None],
    ) -> None:
        timing(DISCONNECT_GRACE_SECONDS=0.3)
        await rig.play_for("101", 30)
        states: list[ConnectionState] = []
        rig.conn.add_status_listener(lambda status: states.append(status.state))

        rig.server.drop_connections()
        await rig.wait(lambda: ConnectionState.DISCONNECTED in states and rig.conn.is_connected)
        await rig.idle(0.4)

        assert rig.cb.endings() == NOTHING_FIRED
        assert await rig.backend.get_state() == PlaybackState.PLAYING

    async def test_disconnect_while_paused_stays_paused(
        self,
        rig: Rig,
        timing: Callable[..., None],
    ) -> None:
        timing(DISCONNECT_GRACE_SECONDS=0.1)
        await rig.play_for("101", 30)
        await rig.backend.pause()

        rig.server.set_reachable(False, drop=True)
        await rig.idle(0.3)

        assert rig.cb.endings() == NOTHING_FIRED
        assert await rig.backend.get_state() == PlaybackState.PAUSED

    async def test_ma_restart_with_players_back_late_interrupts(self, rig: Rig) -> None:
        await rig.play_for("101", 60)

        rig.server.restart()  # MA stops every queue
        await rig.wait(lambda: not rig.conn.is_connected)
        rig.server.come_back(players_available=False)
        await rig.wait(lambda: rig.conn.is_connected)

        await rig.wait(lambda: rig.cb.interrupted.called, what="interrupted")
        rig.server.update_player(QID, available=True)  # the speaker registers again
        await rig.idle(0.3)
        assert rig.cb.endings() == {**NOTHING_FIRED, "interrupted": 1}
        assert rig.cb.interrupted.call_args.args[0] == pytest.approx(60_000, abs=1_500)
        assert await rig.backend.get_state() == PlaybackState.STOPPED

    async def test_ma_restart_without_our_player_interrupts_after_the_grace(self, rig: Rig) -> None:
        await rig.play_for("101", 60)

        rig.server.restart()
        await rig.wait(lambda: not rig.conn.is_connected)
        rig.server.remove_player(QID)  # not registered again (yet)
        rig.server.come_back()
        await rig.wait(lambda: rig.conn.is_connected)
        assert rig.conn.queue(QID) is None

        await rig.wait(lambda: rig.cb.interrupted.called, what="interrupted")
        rig.server.add_player(QID, "Kitchen")  # it shows up much later
        await rig.idle(0.3)
        assert rig.cb.endings() == {**NOTHING_FIRED, "interrupted": 1}


# --------------------------------------------------------------------------- detached


class TestDetached:
    async def test_no_callbacks_for_old_items_after_our_stop(self, rig: Rig) -> None:
        await rig.play_for("101", 30)
        assert await rig.arm("102")
        await rig.backend.stop()

        rig.server.ui_resume(QID)  # someone presses play in MA
        await rig.settle()
        rig.server.finish_current_track(QID)  # 101 -> 102
        await rig.settle()
        rig.server.finish_current_track(QID)  # 102 ends
        await rig.settle()
        rig.server.mark_ended(QID)
        await rig.idle(0.3)

        assert rig.cb.endings() == NOTHING_FIRED
        assert rig.server.writes.count(("player_queues/stop", {"queue_id": QID})) == 1

    async def test_no_callbacks_after_an_interruption_while_ma_plays_on(self, rig: Rig) -> None:
        await rig.play_for("101", 30)
        assert await rig.arm("102")
        rig.server.ui_stop(QID)
        await rig.wait(lambda: rig.cb.interrupted.called)

        rig.server.ui_resume(QID)
        await rig.settle()
        rig.server.finish_current_track(QID)  # reaches the item we had armed
        await rig.settle()
        rig.server.finish_current_track(QID)
        await rig.settle()
        rig.server.mark_ended(QID)
        await rig.idle(0.3)

        assert rig.cb.endings() == {**NOTHING_FIRED, "interrupted": 1}
        assert rig.calls("player_queues/play_index") == []


# ---------------------------------------------------------------------- add-on restart


class TestAddonRestart:
    async def _play_before_restart(self, rig: Rig, arm: str = "") -> dict[str, Any]:
        """The old bridge process plays 101 for 30 s (and arms ``arm``); returns its session."""
        saved: dict[str, Any] = {}
        before, _ = rig.make_backend(save_session=lambda s: saved.update(session=s))
        await before.connect()
        await rig.play_for("101", 30, backend=before)
        if arm:
            assert await rig.arm(arm, backend=before)
        await before.disconnect()
        return json.loads(json.dumps(saved["session"]))  # as kept in the settings file

    async def _restart_addon(
        self, rig: Rig, session: dict[str, Any]
    ) -> tuple[MusicAssistantBackend, Callbacks]:
        """The new bridge process: its own connection and a backend with the saved session."""
        conn = rig.new_connection()
        await conn.start()
        await rig.wait(lambda: conn.is_connected)
        backend, cb = rig.make_backend(session=session, conn=conn)
        await backend.connect()
        return backend, cb

    async def test_restored_session_is_detached_and_silent(self, rig: Rig) -> None:
        session = await self._play_before_restart(rig, arm="102")

        after, cb = await self._restart_addon(rig, session)

        assert after._current is not None and after._current.track_id == "101"
        assert after._attached is False
        assert await after.get_state() == PlaybackState.STOPPED
        rig.server.ui_pause(QID)
        rig.server.ui_resume(QID)
        rig.server.finish_current_track(QID)  # MA moves on to 102 by itself
        await rig.settle()
        rig.server.finish_current_track(QID)
        rig.server.mark_ended(QID)
        await rig.idle(0.3)

        assert cb.endings() == NOTHING_FIRED
        assert cb.state.call_count == 0

    async def test_session_from_another_version_is_ignored(self, rig: Rig) -> None:
        backend, _cb = rig.make_backend(session={"current": {"item_id": "x", "old": 1}})
        assert await backend.connect() is True
        assert backend._current is None
        assert await backend.get_state() == PlaybackState.STOPPED

    async def test_play_of_the_same_track_adopts_and_skips_a_stale_seek(self, rig: Rig) -> None:
        session = await self._play_before_restart(rig)
        after, cb = await self._restart_addon(rig, session)
        rig.server.advance_time(QID, 10)  # the restart took 10 s; MA played on
        media_calls = len(rig.calls("player_queues/play_media"))

        await after.play(STREAM_URL, meta("101"))
        await after.seek(30_000)  # the Qobuz app's position from before the restart
        await rig.settle()

        assert len(rig.calls("player_queues/play_media")) == media_calls
        assert rig.calls("player_queues/seek") == []
        assert rig.queue().corrected_elapsed_time == pytest.approx(40, abs=1)
        cb.position.assert_called()
        assert cb.position.call_args.args[0] == pytest.approx(40_000, abs=1_500)
        assert await after.get_state() == PlaybackState.PLAYING
        await rig.idle(0.2)
        assert cb.endings() == NOTHING_FIRED

    async def test_adopting_a_paused_track_resumes_it(self, rig: Rig) -> None:
        session = await self._play_before_restart(rig)
        rig.server.ui_pause(QID)
        after, _cb = await self._restart_addon(rig, session)
        media_calls = len(rig.calls("player_queues/play_media"))

        await after.play(STREAM_URL, meta("101"))
        await rig.settle()

        assert len(rig.calls("player_queues/play_media")) == media_calls
        assert len(rig.calls("player_queues/play")) == 1
        assert rig.ma_state() == MAState.PLAYING

    async def test_same_track_right_after_our_stop_restarts(self, rig: Rig) -> None:
        await rig.play_for("101", 50)
        await rig.backend.stop()

        await rig.play("101")

        assert [c["option"] for c in rig.calls("player_queues/play_media")] == ["replace"] * 2
        assert rig.queue().corrected_elapsed_time == pytest.approx(0, abs=1)

    async def test_same_track_right_after_our_stop_restarts_even_if_ma_resumed_it(
        self, rig: Rig
    ) -> None:
        await rig.play_for("101", 50)
        await rig.backend.stop()
        rig.server.ui_resume(QID)  # MA picks it up again at 0:50
        await rig.settle()
        assert rig.ma_state() == MAState.PLAYING

        await rig.play("101")

        assert len(rig.calls("player_queues/play_media")) == 2
        assert rig.queue().corrected_elapsed_time == pytest.approx(0, abs=1)
