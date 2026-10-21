"""Which Music Assistant players and party speakers appear in the Qobuz app, under which name,
uuid and port; how that survives restarts, renames, outages and logins; the settings file and the
panel status.
"""

from __future__ import annotations

import json
import shutil
import stat
from collections.abc import AsyncIterator
from dataclasses import replace
from pathlib import Path
from typing import Any, Callable
from unittest.mock import Mock

import pytest
from music_assistant_models.enums import PlayerType

from qobuz_proxy.backends.music_assistant import LoginError, settings
from qobuz_proxy.backends.music_assistant import speakers as speakers_module
from qobuz_proxy.backends.music_assistant.backend import MusicAssistantBackend
from qobuz_proxy.backends.music_assistant.home_assistant import HomeAssistantError, MediaPlayer
from qobuz_proxy.backends.music_assistant.party import PartyBackend
from qobuz_proxy.backends.music_assistant.settings import Settings
from qobuz_proxy.backends.music_assistant.speakers import (
    PARTY,
    MusicAssistantSpeakers,
    speaker_uuid,
)
from qobuz_proxy.config import SpeakerConfig
from tests.fake_music_assistant import ADMIN_PASSWORD, FakeMusicAssistantServer
from tests.rig import KITCHEN, FakeHomeAssistant, idle, wait_for

EXPOSED = {KITCHEN, "group", "pair"}
BIND_ADDRESS = "192.0.2.10"  # the address the add-on is set to listen on
KITCHEN_UUID = "b4775f8a-b4c8-517a-a942-d6239961083d"  # must never change: the app knows it
SESSION = {"current": {"item_id": "qi1", "track_id": "101"}, "armed": None}


class FakeSpeaker:
    """A speaker as the Qobuz app sees it: a name, uuid and port while it runs."""

    def __init__(self, add_on: AddOn, config: SpeakerConfig, backend: MusicAssistantBackend):
        self.add_on = add_on
        self.config = config
        self.backend = backend
        self.key = backend.player_id  # the player id, or PARTY + the party id
        if isinstance(backend, PartyBackend):
            parties = add_on.saved().party_speakers
            self.key = next(PARTY + i for i in parties if speaker_uuid(PARTY + i) == config.uuid)
        self.status = "idle"
        self.running = False

    async def start(self) -> bool:
        self.add_on.starts.append(self.key)
        if self.add_on.failing_starts.get(self.key, 0):
            self.add_on.failing_starts[self.key] -= 1
            return False
        self.running = True
        return True

    async def stop(self) -> None:
        self.running = False

    def get_status(self) -> dict[str, Any]:
        return {"status": self.status}


class AddOn:
    """The add-on's Music Assistant side, with speakers that only record what the app lists."""

    def __init__(self, server: FakeMusicAssistantServer, path: Path) -> None:
        self.server = server
        self.path = path
        self.busy_ports: set[int] = set()
        self.failing_starts: dict[str, int] = {}  # speaker key -> starts that fail
        self.starts: list[str] = []  # speaker key of every start attempt
        self.built: list[FakeSpeaker] = []
        self.runs: list[MusicAssistantSpeakers] = []
        self.home_assistant = FakeHomeAssistant()

    async def start(self, *, qobuz_logged_in: bool = True) -> MusicAssistantSpeakers:
        music_assistant = MusicAssistantSpeakers(
            self.path,
            self._make_speaker,
            bind_address=BIND_ADDRESS,
            client_factory=self.server.client_factory,
            port_is_free=lambda _address, port: port not in self.busy_ports,
            home_assistant=self.home_assistant,
        )
        self.runs.append(music_assistant)
        await music_assistant.start()
        if qobuz_logged_in:
            await music_assistant.set_qobuz_logged_in(True)
        return music_assistant

    async def stop(self) -> None:
        for music_assistant in self.runs:
            await music_assistant.stop()
        await self.server.settle()

    def _make_speaker(self, config: SpeakerConfig, backend: MusicAssistantBackend) -> FakeSpeaker:
        speaker = FakeSpeaker(self, config, backend)
        self.built.append(speaker)
        return speaker

    def _running(self) -> dict[str, FakeSpeaker]:
        running = [s for s in self.built if s.running]
        keys = [s.key for s in running]
        assert len(keys) == len(set(keys)), f"two speakers for one player: {sorted(keys)}"
        return {s.key: s for s in running}

    def listed(self) -> dict[str, str]:
        """Speaker key -> speaker name, for every speaker the Qobuz app lists now."""
        return {key: s.config.name for key, s in self._running().items()}

    def ports(self) -> dict[str, int]:
        return {key: s.config.http_port for key, s in self._running().items()}

    def speaker(self, key: str) -> FakeSpeaker:
        return self._running()[key]

    def saved(self) -> Settings:
        return settings.load(self.path)

    async def wait_for(self, condition: Callable[[], Any], what: str) -> None:
        await wait_for(condition, what, server=self.server)

    async def wait_until_listed(self, keys: set[str]) -> None:
        await self.wait_for(lambda: set(self.listed()) == keys, f"{sorted(keys)}")

    async def idle(self, seconds: float) -> None:
        await idle(self.server, seconds)


@pytest.fixture
def server(server: FakeMusicAssistantServer) -> FakeMusicAssistantServer:
    """Kitchen, a group, a stereo pair, and three players the app must never list."""
    server.add_player("group", "Downstairs", type=PlayerType.GROUP, group_members=[KITCHEN])
    server.add_player("pair", "Bedroom pair", type=PlayerType.STEREO_PAIR)
    server.add_player("hidden", "Hidden speaker", hide_in_ui=True)
    server.add_player("disabled", "Disabled speaker", enabled=False)
    server.add_player("protocol", "AirPlay protocol", type=PlayerType.PROTOCOL)
    return server


@pytest.fixture
async def add_on(
    server: FakeMusicAssistantServer, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, fake_login
) -> AsyncIterator[AddOn]:
    """Connected to Music Assistant in an earlier run, not started yet."""
    monkeypatch.setattr(speakers_module, "UPDATE_DELAY_SECONDS", 0.01)
    monkeypatch.setattr(speakers_module, "START_RETRY_DELAYS_SECONDS", (0.05,))
    add_on = AddOn(server, tmp_path / "data" / "music_assistant.json")
    settings.save(add_on.path, Settings(url=server.url, token=server.token, username="admin"))
    try:
        yield add_on
    finally:
        await add_on.stop()


def _panel_row(music_assistant: MusicAssistantSpeakers, player_id: str) -> dict[str, Any]:
    rows = music_assistant.status()["players"]
    return next(row for row in rows if row["player_id"] == player_id)


async def test_every_visible_player_group_and_stereo_pair_appears_in_the_app_under_a_unique_name(
    server, add_on
):
    music_assistant = await add_on.start()
    await add_on.wait_until_listed(EXPOSED)
    server.add_player("up2", "kitchen")  # the same name as Kitchen but for the case
    await add_on.wait_until_listed(EXPOSED | {"up2"})

    assert add_on.listed() == {
        KITCHEN: "Kitchen (MA)",
        "up2": "kitchen (MA) 2",
        "group": "Downstairs (MA)",
        "pair": "Bedroom pair (MA)",
    }
    kitchen = add_on.speaker(KITCHEN).config
    # Music Assistant reports plays to Qobuz itself; its kind of speaker does not do it again
    assert (kitchen.backend_type, kitchen.bind_address) == ("music_assistant", BIND_ADDRESS)
    # The panel lists the hidden player too, so it can be ticked; never the disabled one or
    # the protocol player
    rows = music_assistant.status()["players"]
    assert [(p["name"], p["type"], p["hidden"], p["speaker_name"]) for p in rows] == [
        ("Bedroom pair", "stereo_pair", False, "Bedroom pair (MA)"),
        ("Downstairs", "group", False, "Downstairs (MA)"),
        ("Hidden speaker", "player", True, ""),
        ("Kitchen", "player", False, "Kitchen (MA)"),
        ("kitchen", "player", False, "kitchen (MA) 2"),
    ]
    assert all(p["available"] for p in rows)

    server.update_player("pair", available=False)  # powered off: the panel marks it offline
    await add_on.wait_for(lambda: not _panel_row(music_assistant, "pair")["available"], "offline")


async def test_a_speaker_keeps_the_name_the_user_gave_it_until_it_is_cleared(add_on):
    music_assistant = await add_on.start()
    await add_on.wait_until_listed(EXPOSED)

    await music_assistant.set_speaker_name(KITCHEN, "  Küche   oben ")
    assert add_on.listed()[KITCHEN] == "Küche oben"  # as typed, without " (MA)"
    assert add_on.speaker(KITCHEN).config.uuid == KITCHEN_UUID  # the same device in the app
    for name, problem in [
        ("Downstairs (MA)", "already called"),
        ("küche OBEN", "already called"),
        ("x" * 41, "40 characters"),
        ("Bad\x00name", "letters, numbers"),
    ]:
        with pytest.raises(ValueError, match=problem):
            await music_assistant.set_speaker_name("pair", name)

    await add_on.stop()  # and the add-on restarts
    music_assistant = await add_on.start()
    await add_on.wait_until_listed(EXPOSED)
    assert add_on.listed()[KITCHEN] == "Küche oben"

    await music_assistant.set_speaker_name(KITCHEN, "")
    assert add_on.listed()[KITCHEN] == "Kitchen (MA)"


async def test_ticked_players_are_the_only_ones_in_the_app_and_stay_ticked(add_on):
    music_assistant = await add_on.start()
    await add_on.wait_until_listed(EXPOSED)
    downstairs = add_on.speaker("group")

    # A hidden player can be ticked too; a player ticked twice is still one speaker
    await music_assistant.set_exposed_players(
        expose_all=False, selected=["group", "hidden", "group"]
    )

    assert add_on.listed() == {"group": "Downstairs (MA)", "hidden": "Hidden speaker (MA)"}
    assert add_on.speaker("group") is downstairs  # left alone, not restarted
    ticked = [p["player_id"] for p in music_assistant.status()["players"] if p["selected"]]
    assert ticked == ["group", "hidden"]

    await add_on.stop()  # and the add-on restarts
    await add_on.start()
    await add_on.wait_until_listed({"group", "hidden"})


async def test_speakers_appear_only_while_qobuz_is_logged_in(add_on):
    music_assistant = await add_on.start(qobuz_logged_in=False)
    await add_on.wait_for(lambda: music_assistant.connection.is_connected, "Music Assistant")
    await add_on.idle(0.05)
    assert add_on.listed() == {}

    await music_assistant.set_qobuz_logged_in(True)
    assert set(add_on.listed()) == EXPOSED
    kitchen = add_on.speaker(KITCHEN)

    # Another Qobuz login: the speakers are built again for the new account
    await music_assistant.set_qobuz_logged_in(True, restart=True)
    assert set(add_on.listed()) == EXPOSED and add_on.speaker(KITCHEN) is not kitchen

    await music_assistant.set_qobuz_logged_in(False)
    assert add_on.listed() == {}


async def test_a_renamed_player_keeps_its_speaker_and_waits_until_the_music_stops(server, add_on):
    await add_on.start()
    await add_on.wait_until_listed(EXPOSED)
    kitchen = add_on.speaker(KITCHEN)
    kitchen.status = "playing"

    server.update_player(KITCHEN, name="Cuisine")
    await add_on.idle(0.1)
    kitchen.status = "paused"
    server.set_player_volume("group", 20)  # any Music Assistant event checks the names again
    await add_on.idle(0.1)
    assert add_on.speaker(KITCHEN) is kitchen  # a rename restarts the speaker: not mid-song
    assert add_on.listed()[KITCHEN] == "Kitchen (MA)"

    kitchen.status = "idle"
    server.set_player_volume("group", 21)
    await add_on.wait_for(lambda: add_on.listed().get(KITCHEN) == "Cuisine (MA)", "the new name")
    renamed = add_on.speaker(KITCHEN).config
    assert (renamed.uuid, renamed.http_port) == (KITCHEN_UUID, kitchen.config.http_port)


async def test_after_a_restart_each_speaker_keeps_its_port_and_session(server, add_on):
    add_on.busy_ports = {8701}
    await add_on.start()
    await add_on.wait_until_listed(EXPOSED)
    ports = add_on.ports()
    assert ports == {KITCHEN: 8700, "group": 8702, "pair": 8703}
    # What the backend saves as it plays (playing for real would need the whole Qobuz side)
    add_on.speaker(KITCHEN).backend._save_session_callback(SESSION)
    assert add_on.saved().sessions == {KITCHEN: SESSION}
    await add_on.stop()

    # On the next start those ports are taken for a moment, and a new player turned up
    add_on.busy_ports = set(ports.values())
    server.add_player("up3", "Bathroom")
    await add_on.start()
    await add_on.wait_until_listed(EXPOSED | {"up3"})

    assert add_on.ports() == {**ports, "up3": 8701}  # free again, and no known player has it
    assert add_on.speaker(KITCHEN).backend._saved_session == SESSION
    assert add_on.speaker("group").backend._saved_session is None


async def test_known_players_stay_in_the_app_while_music_assistant_is_down(
    server, add_on, monkeypatch
):
    # However long it is down: longer even than a removed player is kept
    monkeypatch.setattr(speakers_module, "MISSING_PLAYER_GRACE_SECONDS", 0.05)
    # As an earlier run (of an older version, with a setting that is gone now) saved it
    known = {
        KITCHEN: {"name": "Kitchen", "player_type": "player", "http_port": 8700},
        "group": {"name": "Downstairs", "player_type": "group", "http_port": 8701},
        "hidden": {"name": "Hidden", "player_type": "player", "http_port": 8702, "hidden": True},
    }
    saved = {"url": server.url, "token": server.token, "name_suffix": True, "known_players": known}
    add_on.path.write_text(json.dumps(saved))
    server.set_reachable(False)

    music_assistant = await add_on.start()
    assert add_on.ports() == {KITCHEN: 8700, "group": 8701}
    await add_on.idle(0.15)  # many failed reconnects later
    assert add_on.listed() == {KITCHEN: "Kitchen (MA)", "group": "Downstairs (MA)"}
    assert _panel_row(music_assistant, KITCHEN)["available"] is False

    server.set_reachable(True)  # the same speakers stay, the rest join
    await add_on.wait_until_listed(EXPOSED)
    assert add_on.starts.count(KITCHEN) == 1


async def test_a_player_removed_from_music_assistant_stays_for_a_grace_period_then_is_forgotten(
    server, add_on, monkeypatch
):
    monkeypatch.setattr(speakers_module, "MISSING_PLAYER_GRACE_SECONDS", 0.3)
    settings.save(add_on.path, replace(add_on.saved(), sessions={"pair": SESSION}))
    music_assistant = await add_on.start()
    await add_on.wait_until_listed(EXPOSED)
    party_id = await music_assistant.add_party_speaker(
        name="Bedroom party", main_player_id="pair", member_ids=[KITCHEN]
    )

    server.remove_player("pair")
    await add_on.idle(0.05)
    assert "pair" in add_on.listed()  # Music Assistant may just be registering it again
    assert _panel_row(music_assistant, "pair")["available"] is False

    await add_on.idle(0.3)
    server.set_player_volume(KITCHEN, 25)  # any later Music Assistant event checks again
    await add_on.wait_until_listed({KITCHEN, "group"})  # and the party it played gone with it
    saved = add_on.saved()
    assert "pair" not in saved.known_players and "pair" not in saved.sessions
    assert party_id in saved.party_speakers  # until the user deletes it, keeping its name
    with pytest.raises(ValueError, match="already called"):
        await music_assistant.set_speaker_name(KITCHEN, "Bedroom party")


async def test_a_speaker_that_fails_to_start_is_retried_and_never_doubled(
    server, add_on, monkeypatch
):
    monkeypatch.setattr(speakers_module, "START_RETRY_DELAYS_SECONDS", (0.5, 0.05))
    add_on.failing_starts.update({KITCHEN: 2, "pair": 1})  # e.g. its port is still taken
    music_assistant = await add_on.start()
    await add_on.wait_until_listed({"group"})
    assert music_assistant.status()["speaker_count"] == 1  # the panel counts running ones

    # Music Assistant events while the retry waits must not start a second Kitchen
    for level in (11, 12, 13):
        server.set_player_volume("group", level)
        await add_on.idle(0.03)
    await music_assistant.update_speakers()
    assert add_on.starts.count(KITCHEN) == 1

    # The pair is unticked while its retry waits: it must not turn up in the app later
    await music_assistant.set_exposed_players(expose_all=False, selected=[KITCHEN, "group"])
    await add_on.wait_until_listed({KITCHEN, "group"})
    await add_on.idle(0.2)
    assert add_on.starts.count(KITCHEN) == 3  # two failures, then it runs
    assert add_on.starts.count("pair") == 1


async def test_connecting_saves_a_new_token_and_disconnecting_forgets_it(server, add_on):
    shutil.rmtree(add_on.path.parent)  # a fresh install
    music_assistant = await add_on.start()
    assert music_assistant.status()["is_configured"] is False

    await music_assistant.connect(" 127.0.0.1:8095// ", " admin ", ADMIN_PASSWORD)  # as typed

    saved = add_on.saved()
    assert saved.token in server.valid_tokens and saved.token != server.token
    assert stat.S_IMODE(add_on.path.stat().st_mode) == 0o600  # it holds the token
    status = music_assistant.status()
    assert status["connection"] == {
        "state": "connected",
        "message": "Connected to Music Assistant 2.10.7.",
        "server_version": "2.10.7",
        "has_qobuz_provider": True,
        "url": "http://127.0.0.1:8095",
        "username": "admin",
    }
    assert saved.token not in json.dumps(status)
    assert set(add_on.listed()) == EXPOSED

    await music_assistant.set_exposed_players(expose_all=False, selected=["group"])
    await music_assistant.disconnect()
    await add_on.idle(0.05)  # the speakers must not come back

    saved = add_on.saved()
    assert (saved.url, saved.token) == (server.url, "")
    assert (saved.expose_all, saved.selected_player_ids) == (False, ["group"])
    assert add_on.listed() == {}
    assert music_assistant.status()["is_configured"] is False

    # Connecting again, with every player already known: the new token is saved too
    await music_assistant.connect(server.url, "admin", ADMIN_PASSWORD)
    assert add_on.saved().token in server.valid_tokens
    assert add_on.listed() == {"group": "Downstairs (MA)"}


async def test_login_problems_come_back_as_plain_messages_and_save_nothing(server, add_on):
    shutil.rmtree(add_on.path.parent)
    music_assistant = await add_on.start()
    server.users["visitor"] = "visitor-password"
    server.guest_users.add("visitor")

    async def problem(username: str = "admin", password: str = "typo", url: str = server.url):
        with pytest.raises(LoginError) as err:
            await music_assistant.connect(url, username, password)
        return str(err.value)

    assert "Enter your Music Assistant username and password" in await problem(password="")
    message = await problem()
    assert "did not accept that username and password" in message and "typo" not in message
    assert "is a guest" in await problem("visitor", "visitor-password")
    server.setup_required = True
    assert "not set up yet" in await problem(password=ADMIN_PASSWORD)
    server.set_reachable(False)
    # An empty address means Music Assistant's usual one
    assert "Can't reach Music Assistant at http://127.0.0.1:8095" in await problem(url="")
    assert not add_on.path.exists()
    connection = music_assistant.status()["connection"]
    # The panel still offers the usual address for the next try
    assert (connection["state"], connection["url"]) == ("not_configured", "http://127.0.0.1:8095")


async def test_a_party_speaker_appears_under_its_name_with_its_own_uuid_and_port_after_restarts(
    server, add_on
):
    music_assistant = await add_on.start()
    await add_on.wait_until_listed(EXPOSED)
    party_id = await music_assistant.add_party_speaker(
        name=" Garden   party ", main_player_id=KITCHEN, member_ids=["pair"]
    )
    party = PARTY + party_id

    assert add_on.listed()[party] == "Garden party"  # as typed, without " (MA)"
    config = add_on.speaker(party).config
    assert config.uuid == speaker_uuid(party)
    assert config.http_port not in {p.http_port for p in add_on.saved().known_players.values()}
    await add_on.speaker(party).backend.party.gather()  # as its first play does
    assert music_assistant.status()["party_speakers"] == [
        {
            "party_id": party_id,
            "name": "Garden party",
            "main_player_id": KITCHEN,
            "grouping": "music_assistant",
            "member_ids": ["pair"],
            "main_entity_id": "",
            "busy_speakers": "join",
            "after_party": "split",
            "on_leave": "split",
            "speaker_name": "Garden party",
            "problem": "Bedroom pair could not join: Player Kitchen does not support group "
            "commands.",
        }
    ]

    server.add_player("up3", "Bathroom")
    await add_on.wait_until_listed(EXPOSED | {party, "up3"})
    assert len(set(add_on.ports().values())) == len(EXPOSED) + 2

    # It stays while its main player is known, whichever players are ticked
    await music_assistant.set_exposed_players(expose_all=False, selected=[])
    await add_on.stop()  # and the add-on restarts
    await add_on.start()
    await add_on.wait_until_listed({party})
    assert (add_on.speaker(party).config.uuid, add_on.ports()) == (config.uuid, {party: 8704})


async def test_a_party_can_play_on_a_player_that_is_not_in_the_app_itself(add_on):
    # e.g. a speaker with Qobuz Connect built in, left out so it does not show up twice, that
    # still has to lead the party
    music_assistant = await add_on.start()
    await add_on.wait_until_listed(EXPOSED)
    await music_assistant.set_exposed_players(expose_all=False, selected=[KITCHEN])
    unticked = await music_assistant.add_party_speaker(
        name="Partymodus", main_player_id="pair", member_ids=[KITCHEN]
    )
    await music_assistant.set_exposed_players(expose_all=True)  # leaves the hidden player out
    hidden = await music_assistant.add_party_speaker(
        name="Hidden party", main_player_id="hidden", member_ids=[KITCHEN]
    )
    await music_assistant.set_exposed_players(expose_all=False, selected=[KITCHEN])

    assert add_on.listed() == {
        KITCHEN: "Kitchen (MA)",
        PARTY + unticked: "Partymodus",
        PARTY + hidden: "Hidden party",
    }


async def test_a_changed_party_speaker_restarts_once_its_music_stops_and_a_deleted_one_goes(
    server, add_on
):
    music_assistant = await add_on.start()
    await add_on.wait_until_listed(EXPOSED)
    party_id = await music_assistant.add_party_speaker(
        name="Party", main_player_id=KITCHEN, member_ids=["pair"]
    )
    party = PARTY + party_id
    speaker = add_on.speaker(party)
    speaker.status = "playing"
    speaker.backend._save_session_callback(SESSION)

    await music_assistant.update_party_speaker(party_id, member_ids=["hidden"], on_leave="keep")
    assert add_on.speaker(party) is speaker  # a change restarts the speaker: not mid-song
    speaker.status = "idle"
    server.set_player_volume(KITCHEN, 20)  # any Music Assistant event checks again
    await add_on.wait_for(
        lambda: party in add_on.listed() and add_on.speaker(party) is not speaker, "a restart"
    )
    changed = add_on.speaker(party)
    assert changed.backend.party.party_speaker.member_ids == ["hidden"]
    assert (changed.config.uuid, changed.config.http_port) == (
        speaker.config.uuid,
        speaker.config.http_port,
    )
    assert changed.backend._saved_session == SESSION

    await music_assistant.remove_party_speaker(party_id)
    assert set(add_on.listed()) == EXPOSED
    assert (add_on.saved().party_speakers, add_on.saved().sessions) == ({}, {})
    with pytest.raises(ValueError, match="not there any more"):
        await music_assistant.update_party_speaker(party_id, name="Party again")


async def test_a_party_speaker_is_checked_before_it_is_saved(server, add_on):
    music_assistant = await add_on.start()
    await add_on.wait_until_listed(EXPOSED)
    server.update_player(KITCHEN, can_group_with=["pair", "group"])
    await add_on.wait_for(
        lambda: _panel_row(music_assistant, KITCHEN)["can_group_with"] == ["group", "pair"],
        "Kitchen's sync partners in the panel",
    )
    await music_assistant.set_speaker_name("pair", "Patio")
    good = {"name": "Party", "main_player_id": KITCHEN, "member_ids": ["pair"]}
    home_assistant = {"grouping": "home_assistant", "main_entity_id": "media_player.kitchen"}
    for fields, problem in [
        ({"name": " "}, "Give the party speaker a name"),
        ({"name": "x" * 41}, "40 characters"),
        ({"name": "Bad\x00name"}, "letters, numbers"),
        ({"name": "downstairs (ma)"}, "already called"),
        ({"name": "PATIO"}, "already called"),
        ({"main_player_id": "gone"}, "not known"),
        ({"member_ids": []}, "at least one speaker"),
        ({"member_ids": [KITCHEN]}, "main speaker can't also"),
        ({"member_ids": ["hidden"]}, "Hidden speaker can't sync with Kitchen"),
        ({"grouping": "bluetooth"}, "who groups"),
        ({"busy_speakers": "maybe"}, "already playing"),
        ({"after_party": "dance"}, "when the music stops"),
        ({"on_leave": "dance"}, "another device"),
        ({**home_assistant, "main_entity_id": "light.porch"}, "main speaker in Home Assistant"),
        ({**home_assistant, "member_ids": ["switch.fan"]}, "media players to join"),
        ({"member_ids": [7]}, "Invalid field: member_ids"),
        ({"colour": "red"}, "Invalid field: colour"),
    ]:
        with pytest.raises(ValueError, match=problem):
            await music_assistant.add_party_speaker(**{**good, **fields})
    party_id = await music_assistant.add_party_speaker(**good, main_entity_id="media_player.x")
    assert add_on.saved().party_speakers[party_id].main_entity_id == ""  # Home Assistant only

    with pytest.raises(ValueError, match="already called"):
        await music_assistant.set_speaker_name("group", "party")
    add_on.home_assistant.available = False  # the add-on runs outside Home Assistant
    with pytest.raises(ValueError, match="runs in Home Assistant"):
        await music_assistant.update_party_speaker(
            party_id, **home_assistant, member_ids=["media_player.hedwig"]
        )
    assert [p.name for p in add_on.saved().party_speakers.values()] == ["Party"]


async def test_the_party_form_lists_home_assistant_players_only_inside_home_assistant(add_on):
    add_on.home_assistant.players = {
        p.entity_id: p
        for p in [
            MediaPlayer("media_player.radio", "radio", "off", False, []),
            MediaPlayer("media_player.hedwig", "Hedwig", "idle", True, []),
        ]
    }
    music_assistant = await add_on.start()
    assert music_assistant.status()["home_assistant_available"] is True
    assert await music_assistant.home_assistant_players() == [
        {"entity_id": "media_player.hedwig", "name": "Hedwig", "can_group": True},
        {"entity_id": "media_player.radio", "name": "radio", "can_group": False},
    ]

    add_on.home_assistant.available = False
    assert music_assistant.status()["home_assistant_available"] is False
    with pytest.raises(HomeAssistantError, match="runs in Home Assistant"):
        await music_assistant.home_assistant_players()


def test_a_failed_save_keeps_the_previous_settings_file(tmp_path, monkeypatch):
    path = tmp_path / "music_assistant.json"
    settings.save(path, Settings(url="http://old:8095"))

    with monkeypatch.context() as patch:
        patch.setattr(settings.os, "replace", Mock(side_effect=OSError("disk full")))
        settings.save(path, Settings(url="http://new:8095"))  # logged, not raised

    assert settings.load(path).url == "http://old:8095"
    assert [p.name for p in tmp_path.iterdir()] == [path.name]  # no temporary file left


def test_a_missing_or_unreadable_settings_file_gives_the_defaults(tmp_path):
    path = tmp_path / "music_assistant.json"
    assert settings.load(path) == Settings()
    for content in [
        "{not json",
        '["url"]',
        json.dumps({"known_players": {KITCHEN: {"name": "K"}}}),
    ]:
        path.write_text(content)
        assert settings.load(path) == Settings(), content
