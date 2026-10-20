"""What this repo changes inside upstream QobuzProxy files (speaker.py, playback/player.py, app.py).

Run these after merging upstream: each one fails if the merge lost one of those changes.
"""

import asyncio
import contextlib
import functools
from collections.abc import AsyncIterator
from typing import Any
from unittest.mock import MagicMock

import aiohttp
import pytest
from music_assistant_models.enums import PlaybackState as MusicAssistantState

from qobuz_proxy import speaker as speaker_module
from qobuz_proxy.app import QobuzProxy
from qobuz_proxy.backends import BackendTrackMetadata, PlaybackState
from qobuz_proxy.backends.base import AudioBackend
from qobuz_proxy.backends.music_assistant import connection as connection_module
from qobuz_proxy.backends.music_assistant import settings
from qobuz_proxy.backends.music_assistant import speakers as speakers_module
from qobuz_proxy.config import Config, QobuzConfig, ServerConfig, SpeakerConfig
from qobuz_proxy.playback.player import QobuzPlayer
from qobuz_proxy.playback.queue import QobuzQueue
from qobuz_proxy.proto import qconnect_payload_pb2 as pb
from qobuz_proxy.webui.config_writer import save_config
from tests.fake_music_assistant import FakeMusicAssistantServer
from tests.fake_qobuz import (
    APP_PLAYING,
    APP_STOPPED,
    StubDiscovery,
    StubMetadata,
    album_state,
    set_active,
    set_volume,
)
from tests.rig import PID, Rig, wait_for

CD_QUALITY = 2  # what the app sends for CD quality; Qobuz calls it format 6


def _volume_broadcast(level: int) -> Any:
    """The cloud telling every device in the session that the volume changed (no renderer id)."""
    msg = pb.QConnectMessage(messageType=87)
    msg.srvrCtrlVolumeChanged.volume = level
    return msg


def _max_quality(level: int) -> Any:
    msg = pb.QConnectMessage(messageType=44)
    msg.srvrRndrSetMaxAudioQuality.maxAudioQuality = level
    return msg


def _volumes_set_in_music_assistant(rig: Rig) -> list[int]:
    return [call["volume_level"] for call in rig.server.calls("players/cmd/group_volume")]


async def test_a_volume_broadcast_reaches_only_the_active_speaker_but_a_direct_command_always_does(
    rig,
):
    await rig.deliver(_volume_broadcast(25))
    await rig.wait_for(lambda: _volumes_set_in_music_assistant(rig) == [25], "volume 25")

    await rig.deliver(set_active(False))  # the app now plays on another device
    await rig.deliver(_volume_broadcast(60))
    await rig.deliver(set_volume(45))  # addressed to this speaker
    await rig.wait_for(lambda: _volumes_set_in_music_assistant(rig)[-1] == 45, "volume 45")

    assert _volumes_set_in_music_assistant(rig) == [25, 45]


async def test_a_volume_change_in_music_assistant_shows_in_the_app_only_while_the_speaker_is_active(
    rig,
):
    rig.server.set_player_volume(PID, 40)
    await rig.wait_for(lambda: 40 in rig.cloud.volumes, "the app shows volume 40")

    await rig.deliver(set_active(False))
    rig.server.set_player_volume(PID, 70)
    await rig.idle(0.2)

    assert 70 not in rig.cloud.volumes


async def test_playing_from_a_position_starts_music_assistant_there_without_a_seek(rig):
    await rig.deliver(album_state(0, APP_PLAYING, pos=61_000))
    await rig.wait_for(
        lambda: rig.queue_state() == MusicAssistantState.PLAYING, "Music Assistant playing"
    )

    assert rig.server.queue(PID).corrected_elapsed_time == pytest.approx(61, abs=1)
    assert rig.server.calls("player_queues/seek") == []


async def test_a_track_music_assistant_cannot_play_is_skipped_when_play_is_pressed_on_it(rig):
    rig.server.unplayable_track_ids = {"102"}
    await rig.deliver(album_state(1, APP_STOPPED))  # the app has track 2 up, stopped

    await rig.deliver(album_state(1, APP_PLAYING))

    await rig.wait_for(
        lambda: rig.queue_track() == "103" and rig.app() == (APP_PLAYING, 3), "track 3"
    )
    assert rig.queue_state() == MusicAssistantState.PLAYING


async def test_music_assistant_streams_the_track_itself_so_the_add_on_never_restarts_or_reports_it(
    rig,
):
    await rig.play_album(0)
    await rig.wait_armed("102")
    rig.server.clear_logs()

    await rig.deliver(_max_quality(CD_QUALITY))
    await rig.idle(0.2)

    assert rig.metadata.max_qualities == [6]  # the speaker took the app's choice...
    assert rig.server.writes == []  # ...without restarting the track for it
    assert rig.app() == (APP_PLAYING, 1)
    # Music Assistant's Qobuz provider reports every play; a second report would count it twice.
    # The Qobuz API client the rig gave the speaker is the only place a report would show.
    assert not rig.speaker._api_client.report_streaming_start.called


class Renderer(AudioBackend):
    """A device whose state the test sets, counting how often the player polls it."""

    def __init__(self, reports_external_resume: bool) -> None:
        super().__init__(name="renderer")
        self._reports_external_resume = reports_external_resume
        self.state = PlaybackState.STOPPED
        self.polls = 0

    @property
    def reports_external_resume(self) -> bool:
        return self._reports_external_resume

    async def get_state(self) -> PlaybackState:
        self.polls += 1
        return self.state

    async def play(self, url: str, metadata: BackendTrackMetadata) -> None:
        self.state = PlaybackState.PLAYING

    async def pause(self) -> None:
        self.state = PlaybackState.PAUSED

    async def resume(self) -> bool:
        self.state = PlaybackState.PLAYING
        return True

    async def stop(self, *, next_track_id: str | None = None) -> None:
        self.state = PlaybackState.STOPPED

    async def seek(self, position_ms: int) -> None: ...

    async def get_position(self) -> int:
        return 0

    async def set_volume(self, level: int) -> None: ...

    async def get_volume(self) -> int:
        return 50

    async def connect(self) -> bool:
        self._is_connected = True
        return True

    async def disconnect(self) -> None: ...


@pytest.fixture
def fast_polls(monkeypatch: pytest.MonkeyPatch) -> None:
    """No sleep lasts over 1 ms, so the player polls its device that often instead of every 0.5 s."""
    sleep = asyncio.sleep

    async def short_sleep(delay: float, result: Any = None) -> Any:
        return await sleep(min(delay, 0.001), result)

    monkeypatch.setattr(asyncio, "sleep", short_sleep)


@contextlib.asynccontextmanager
async def _paused_player(renderer: Renderer) -> AsyncIterator[QobuzPlayer]:
    player = QobuzPlayer(queue=QobuzQueue(), metadata_service=StubMetadata(), backend=renderer)
    await player.start()
    try:
        await player.play_track(queue_item_id=1, track_id="101")
        await player.pause()
        yield player
    finally:
        await player.stop()


async def test_a_paused_player_stops_with_its_device_but_resumes_only_if_its_backend_reports_resumes(
    fast_polls,
):
    music_assistant = Renderer(reports_external_resume=True)
    dlna = Renderer(reports_external_resume=False)
    async with _paused_player(music_assistant) as followed, _paused_player(dlna) as ignored:
        music_assistant.state = PlaybackState.PLAYING  # resumed in Music Assistant's own UI
        dlna.state = PlaybackState.PLAYING  # a renderer that still reports a stale state
        await wait_for(lambda: followed.state == PlaybackState.PLAYING, "the resume followed")
        polls = dlna.polls
        await wait_for(lambda: dlna.polls >= polls + 5, "five more polls of the other device")
        assert ignored.state == PlaybackState.PAUSED

        await followed.pause()
        music_assistant.state = PlaybackState.STOPPED  # e.g. its queue was cleared
        await wait_for(lambda: followed.state == PlaybackState.STOPPED, "the player stopped")


@contextlib.asynccontextmanager
async def _add_on(config: Config) -> AsyncIterator[tuple[QobuzProxy, aiohttp.ClientSession]]:
    """The whole add-on running with ``config``, and a client for its web UI."""
    proxy = QobuzProxy(config)
    await proxy.start()
    port = proxy._web_runner.addresses[0][1]  # the free port http_port=0 picked
    try:
        async with aiohttp.ClientSession(f"http://127.0.0.1:{port}") as web_ui:
            yield proxy, web_ui
    finally:
        await proxy.stop()


class _QobuzAcceptingTheSavedLogin:
    def __init__(self, app_id: str, app_secret: str) -> None: ...

    async def login_with_token(self, user_id: str, auth_token: str) -> bool:
        return True


@pytest.fixture
def config_from_an_earlier_run(
    server: FakeMusicAssistantServer, tmp_path, monkeypatch: pytest.MonkeyPatch
) -> Config:
    """Qobuz and Music Assistant were both set up in an earlier run; returns the add-on's config."""
    settings.save(
        tmp_path / "music_assistant.json", settings.Settings(url=server.url, token=server.token)
    )
    monkeypatch.setattr(
        connection_module,
        "MusicAssistantConnection",
        functools.partial(
            connection_module.MusicAssistantConnection, client_factory=server.client_factory
        ),
    )
    monkeypatch.setattr(speakers_module, "UPDATE_DELAY_SECONDS", 0.01)
    monkeypatch.setattr("qobuz_proxy.app.QobuzAPIClient", _QobuzAcceptingTheSavedLogin)
    # A logout clears the saved Qobuz login: keep the developer's own out of reach
    monkeypatch.setattr("qobuz_proxy.auth.credentials.CACHE_FILE", tmp_path / "credentials.json")
    monkeypatch.setattr(speaker_module, "DiscoveryService", StubDiscovery)
    monkeypatch.setattr(speaker_module, "MetadataService", lambda **kw: StubMetadata())
    return Config(
        qobuz=QobuzConfig(user_id="1", auth_token="saved"),
        server=ServerConfig(http_port=0, bind_address="127.0.0.1"),
    )


async def _speakers_in_the_qobuz_app(web_ui: aiohttp.ClientSession) -> int:
    response = await web_ui.get("/api/music-assistant/status")
    return (await response.json())["speaker_count"]


async def test_music_assistant_players_are_in_the_qobuz_app_only_while_the_add_on_runs_logged_in(
    server, config_from_an_earlier_run
):
    async with _add_on(config_from_an_earlier_run) as (_, web_ui):
        await wait_for(lambda: _speakers_in_the_qobuz_app(web_ui), "the Kitchen speaker")
        assert await _speakers_in_the_qobuz_app(web_ui) == 1

        assert (await web_ui.post("/api/auth/logout", json={})).status == 200
        assert await _speakers_in_the_qobuz_app(web_ui) == 0

    await server.settle()
    assert server.clients == []  # the add-on closed its Music Assistant connection


@pytest.fixture
async def web_ui(tmp_path, monkeypatch) -> AsyncIterator[aiohttp.ClientSession]:
    """The add-on's web UI, logged in to Qobuz, with a DLNA speaker saved in config.yaml."""
    monkeypatch.setattr("qobuz_proxy.app.load_user_token", lambda: None)
    config = Config(
        server=ServerConfig(http_port=0, bind_address="127.0.0.1"),
        speakers=[SpeakerConfig(name="Living Room", dlna_ip="192.168.1.20")],
        config_path=tmp_path / "config.yaml",
    )
    save_config(config, config.config_path)
    async with _add_on(config) as (proxy, session):
        # Logged in after start, so the add-on never tries to reach the DLNA speaker
        proxy._api_client = MagicMock()
        yield session


async def test_a_speaker_the_next_start_would_reject_is_refused_in_the_web_ui_and_not_saved(
    web_ui, tmp_path
):
    saved = (tmp_path / "config.yaml").read_text()

    added = await web_ui.post("/api/speakers", json={"name": "Kitchen", "backend": "bogus"})
    edited = await web_ui.put("/api/speakers/living-room", json={"dlna_ip": ""})

    assert (added.status, edited.status) == (400, 400)
    assert "unknown backend type 'bogus'" in (await added.json())["error"]
    assert "DLNA IP address is required" in (await edited.json())["error"]
    assert (tmp_path / "config.yaml").read_text() == saved
    listed = await (await web_ui.get("/api/speakers")).json()
    assert [(s["name"], s["config"]["dlna_ip"]) for s in listed] == [
        ("Living Room", "192.168.1.20")
    ]
