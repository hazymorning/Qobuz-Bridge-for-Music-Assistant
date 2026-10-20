"""Party speakers: the speakers that join the main player before the music starts, and leave it
again when the music stops or the app leaves the party speaker; grouping in Music Assistant or
Home Assistant, what happens when grouping fails, and the Home Assistant client.
"""

import logging
from typing import Any

import pytest
from aiohttp import web
from aiohttp.test_utils import TestServer
from music_assistant_models.enums import PlaybackState as MusicAssistantState
from music_assistant_models.errors import PlayerCommandFailed

from qobuz_proxy.backends.music_assistant import party as party_module
from qobuz_proxy.backends.music_assistant.home_assistant import (
    HomeAssistant,
    HomeAssistantError,
    MediaPlayer,
)
from qobuz_proxy.backends.music_assistant.settings import PartySpeaker
from tests.fake_music_assistant import FakeMusicAssistantServer
from tests.fake_qobuz import (
    APP_PAUSED,
    APP_PLAYING,
    APP_STOPPED,
    album_state,
    set_active,
    set_state,
)
from tests.rig import KITCHEN, FakeHomeAssistant

HEDWIG, BATH, LOFT = "up2", "up3", "up4"
STOP = set_state(APP_STOPPED, track=("101", 1), pos=0)  # the app's stop button


@pytest.fixture
def server(server: FakeMusicAssistantServer) -> FakeMusicAssistantServer:
    """Kitchen, which can sync with Hedwig and Bath but not with Loft."""
    for player_id, name in [(HEDWIG, "Hedwig"), (BATH, "Bath"), (LOFT, "Loft")]:
        server.add_player(player_id, name)
    server.update_player(KITCHEN, can_group_with=[HEDWIG, BATH])
    return server


def party(**options: Any) -> PartySpeaker:
    """A party on Kitchen that Hedwig and Bath join."""
    options = {"member_ids": [HEDWIG, BATH], **options}
    return PartySpeaker(name="Party", main_player_id=KITCHEN, http_port=8800, **options)


def grouped(server: FakeMusicAssistantServer) -> list[str]:
    """The players synced to Kitchen."""
    return [p for p in server.players[KITCHEN].group_members if p != KITCHEN]


def grouping_calls(server: FakeMusicAssistantServer) -> list[tuple[list[str], list[str]]]:
    """(added, removed) of every set_members the add-on sent."""
    return [
        (args.get("player_ids_to_add") or [], args.get("player_ids_to_remove") or [])
        for args in server.calls("players/cmd/set_members")
    ]


async def test_the_speakers_join_before_the_music_starts_and_leave_once_it_has_stopped(
    start_speaker,
):
    rig = await start_speaker(party=party(), name="Party")
    await rig.play_album(0)

    commands = [name for name, _ in rig.server.writes]
    assert commands.index("players/cmd/set_members") < commands.index("player_queues/play_media")
    assert grouped(rig.server) == [HEDWIG, BATH]

    await rig.deliver(STOP)
    assert grouped(rig.server) == [HEDWIG, BATH]  # not before the delay
    await rig.wait_for(lambda: not grouped(rig.server), "the speakers left")
    assert grouping_calls(rig.server) == [([HEDWIG, BATH], []), ([], [HEDWIG, BATH])]


async def test_a_track_change_or_a_pause_keeps_the_party_together(start_speaker):
    rig = await start_speaker(party=party(), name="Party")
    await rig.play_album(0)

    await rig.deliver(album_state(1))  # a stop, then a play
    await rig.wait_for(lambda: rig.queue_track() == "102", "track 2")
    await rig.deliver(album_state(1, APP_PAUSED))
    await rig.idle(0.5)

    assert grouped(rig.server) == [HEDWIG, BATH]
    assert grouping_calls(rig.server) == [([HEDWIG, BATH], [])]


async def test_a_party_that_keeps_the_group_leaves_the_speakers_together(start_speaker):
    rig = await start_speaker(party=party(after_party="keep"), name="Party")
    await rig.play_album(0)
    await rig.deliver(STOP)
    await rig.idle(0.5)
    await rig.play_album(0)

    assert grouped(rig.server) == [HEDWIG, BATH]
    assert grouping_calls(rig.server) == [([HEDWIG, BATH], [])]  # grouped once, never split


async def test_a_party_whose_music_cannot_start_still_splits_the_group(server, start_speaker):
    server.unplayable_track_ids = {"103"}  # e.g. not in Music Assistant's region
    rig = await start_speaker(party=party(), name="Party")
    await rig.deliver(album_state(2))  # the album's last track

    await rig.wait_for(
        lambda: grouping_calls(server) == [([HEDWIG, BATH], []), ([], [HEDWIG, BATH])],
        "the speakers left",
    )


async def test_a_party_speaker_stopped_right_after_its_music_still_splits_the_group(
    start_speaker, monkeypatch
):
    monkeypatch.setattr(party_module, "PARTY_RELEASE_DELAY_SECONDS", 60.0)
    rig = await start_speaker(party=party(), name="Party")
    await rig.play_album(0)
    await rig.deliver(STOP)

    await rig.speaker.stop()  # changed in the panel, or the add-on stops

    assert grouped(rig.server) == []


@pytest.mark.parametrize(
    ("after_party", "on_leave", "release_delay", "still_grouped"),
    [("keep", "split", 60.0, []), ("split", "keep", 0.3, [HEDWIG, BATH])],
)
async def test_leaving_the_party_speaker_in_the_app_splits_the_group_at_once_or_keeps_it(
    start_speaker, monkeypatch, after_party, on_leave, release_delay, still_grouped
):
    monkeypatch.setattr(party_module, "PARTY_RELEASE_DELAY_SECONDS", release_delay)
    rig = await start_speaker(party=party(after_party=after_party, on_leave=on_leave), name="Party")
    await rig.play_album(0)

    await rig.deliver(set_active(False))  # the app plays on another device now
    await rig.idle(0.6)

    assert rig.queue_state() == MusicAssistantState.IDLE  # the music stopped as always
    assert grouped(rig.server) == still_grouped


async def test_only_the_speakers_a_party_grouped_leave_after_it(server, start_speaker):
    rig = await start_speaker(party=party(), name="Party")
    await rig.play_album(0)
    await rig.deliver(STOP)
    await rig.wait_for(lambda: not grouped(server), "the speakers left")

    server.ui_group(KITCHEN, [BATH])  # with Kitchen before the next party
    await rig.idle(0.05)
    await rig.play_album(0)
    await rig.deliver(STOP)
    await rig.wait_for(lambda: grouped(server) == [BATH], "Hedwig left again")

    assert grouping_calls(server)[2:] == [([HEDWIG], []), ([], [HEDWIG])]


@pytest.mark.parametrize(
    ("busy_speakers", "joined"), [("skip", [HEDWIG]), ("join", [HEDWIG, BATH])]
)
async def test_a_speaker_playing_something_else_is_left_playing_or_brought_into_the_party(
    server, start_speaker, busy_speakers, joined
):
    server.start_playing(BATH, ["201"])
    rig = await start_speaker(party=party(busy_speakers=busy_speakers), name="Party")
    await rig.play_album(0)

    assert grouped(server) == joined


async def test_home_assistant_groups_the_party_with_join_and_unjoin(start_speaker):
    kitchen, hedwig, bath, porch = (
        f"media_player.{n}" for n in ["kitchen", "hedwig", "bath", "porch"]
    )
    home_assistant = FakeHomeAssistant(
        MediaPlayer(kitchen, "Kitchen", "idle", True, [kitchen, porch]),
        MediaPlayer(hedwig, "Hedwig", "idle", True, []),
        MediaPlayer(bath, "Bath", "playing", True, []),
        MediaPlayer(porch, "Porch", "idle", True, [kitchen, porch]),
    )
    speaker = party(
        grouping="home_assistant",
        main_entity_id=kitchen,
        member_ids=[hedwig, bath, porch],
        busy_speakers="skip",
    )
    rig = await start_speaker(party=speaker, name="Party", home_assistant=home_assistant)
    await rig.play_album(0)
    await rig.deliver(STOP)
    await rig.wait_for(lambda: len(home_assistant.calls) == 2, "the party split")

    # Bath plays something else, and Porch was with Kitchen before the party
    assert home_assistant.calls == [("join", kitchen, hedwig), ("unjoin", hedwig)]
    assert rig.server.calls("players/cmd/set_members") == []

    home_assistant.failure = "Can't reach Home Assistant."
    await rig.play_album(0)
    assert rig.party.problem == "The speakers could not join: Can't reach Home Assistant."


async def test_a_party_that_cannot_group_still_plays_on_the_main_speaker_and_says_why(
    server, start_speaker
):
    server.update_player(BATH, available=False)
    rig = await start_speaker(party=party(member_ids=[HEDWIG, BATH, LOFT]), name="Party")
    server.fail_next["players/cmd/set_members"] = PlayerCommandFailed("Hedwig is updating")
    await rig.play_album(0)

    assert rig.app() == (APP_PLAYING, 1)
    assert rig.party.problem == (
        "Bath could not join: it is not available in Music Assistant. Loft could not join: it "
        "can't sync with Kitchen. Hedwig could not join: Hedwig is updating."
    )

    await rig.deliver(STOP)
    server.update_player(BATH, available=True)
    server.update_player(KITCHEN, can_group_with=[HEDWIG, BATH, LOFT])
    await rig.idle(0.5)
    await rig.play_album(0)

    assert grouped(server) == [HEDWIG, BATH, LOFT]
    assert rig.party.problem == ""


async def test_the_home_assistant_client_sends_the_token_and_turns_failures_into_plain_messages(
    caplog, monkeypatch
):
    caplog.set_level(logging.DEBUG)
    requests = []

    @web.middleware
    async def record(request: web.Request, handler) -> web.StreamResponse:
        body = await request.json() if request.can_read_body else None
        requests.append((request.method, request.path, body))
        if request.headers.get("Authorization") != "Bearer supervisor-token":
            return web.Response(status=401)
        return await handler(request)

    async def states(request: web.Request) -> web.Response:
        kitchen = {"friendly_name": "Kitchen", "supported_features": 524288 | 1}
        return web.json_response(
            [
                {"entity_id": "media_player.kitchen", "state": "playing", "attributes": kitchen},
                {"entity_id": "media_player.radio", "state": "off", "attributes": {}},
                {"entity_id": "light.porch", "state": "on", "attributes": {}},
            ]
        )

    async def join(request: web.Request) -> web.Response:
        return web.json_response([])

    app = web.Application(middlewares=[record])
    app.router.add_get("/core/api/states", states)
    app.router.add_post("/core/api/services/media_player/join", join)
    async with TestServer(app) as test_server:
        url = str(test_server.make_url("/core/api"))
        home_assistant = HomeAssistant("supervisor-token", url)
        assert await home_assistant.media_players() == [
            MediaPlayer("media_player.kitchen", "Kitchen", "playing", True, []),
            MediaPlayer("media_player.radio", "media_player.radio", "off", False, []),
        ]
        await home_assistant.join("media_player.kitchen", ["media_player.hedwig"])
        with pytest.raises(HomeAssistantError, match="answered 404 Not Found"):
            await home_assistant.unjoin("media_player.hedwig")
        with pytest.raises(HomeAssistantError, match="did not let the add-on in"):
            await HomeAssistant("old-token", url).media_players()
    with pytest.raises(HomeAssistantError, match="Can't reach Home Assistant"):
        await home_assistant.media_players()

    assert requests == [
        ("GET", "/core/api/states", None),
        (
            "POST",
            "/core/api/services/media_player/join",
            {"entity_id": "media_player.kitchen", "group_members": ["media_player.hedwig"]},
        ),
        ("POST", "/core/api/services/media_player/unjoin", {"entity_id": "media_player.hedwig"}),
        ("GET", "/core/api/states", None),
    ]
    assert "supervisor-token" not in caplog.text
    # Outside Home Assistant there is no Supervisor token
    monkeypatch.setenv("SUPERVISOR_TOKEN", "supervisor-token")
    assert HomeAssistant().available
    monkeypatch.delenv("SUPERVISOR_TOKEN")
    with pytest.raises(HomeAssistantError, match="runs in Home Assistant"):
        await HomeAssistant().join("media_player.kitchen", ["media_player.hedwig"])
