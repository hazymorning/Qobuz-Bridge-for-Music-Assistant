"""The add-on panel and its HTTP API: the page, the status checks, the Music Assistant login and
player choice, request validation, and the guard against other sites.
"""

import asyncio
import json
import re
import socket
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any

import pytest
from aiohttp import ClientResponse, web
from aiohttp.test_utils import TestClient, TestServer

from qobuz_proxy.backends.music_assistant import settings
from qobuz_proxy.backends.music_assistant.speakers import MusicAssistantSpeakers
from qobuz_proxy.webui import music_assistant_routes
from qobuz_proxy.webui.routes import register_routes
from tests.fake_music_assistant import ADMIN_PASSWORD, FakeMusicAssistantServer
from tests.rig import KITCHEN, wait_for

STATUS = "/api/music-assistant/status"
CONNECT = "/api/music-assistant/connect"
DISCONNECT = "/api/music-assistant/disconnect"
PLAYERS = "/api/music-assistant/players"
SPEAKER_NAME = "/api/music-assistant/speaker-name"


class SilentSpeaker:
    """Stands in for a Qobuz Connect speaker; the panel only counts them."""

    def __init__(self, config: Any, backend: Any) -> None:
        pass

    async def start(self) -> bool:
        return True

    async def stop(self) -> None:
        pass

    def get_status(self) -> dict[str, Any]:
        return {"status": "idle"}


class Panel:
    """The add-on's web server on a fresh install, with its real Music Assistant side."""

    def __init__(self, server: FakeMusicAssistantServer, settings_path: Path) -> None:
        self.server = server
        self.settings_path = settings_path
        self.auth_state: dict[str, Any] = {"authenticated": False, "email": "", "name": ""}
        self.logouts = 0
        self.music_assistant = MusicAssistantSpeakers(
            settings_path,
            SilentSpeaker,
            client_factory=server.client_factory,
            port_is_free=lambda address, port: True,
        )
        app = web.Application()
        app["auth_state"] = self.auth_state
        app["version"] = "9.9.9"
        app["http_port"] = 8689
        app["music_assistant"] = self.music_assistant
        app["on_logout"] = self._on_logout
        register_routes(app)
        self.client = TestClient(TestServer(app))

    async def _on_logout(self) -> None:
        """What the add-on does when the user logs out of Qobuz."""
        self.logouts += 1
        self.auth_state.update(authenticated=False, email="")
        await self.music_assistant.set_qobuz_logged_in(False)

    async def log_in_to_qobuz(self) -> None:
        """What the add-on does after a restart with a saved Qobuz login (no display name)."""
        self.auth_state.update(authenticated=True, email="alice@example.com")
        await self.music_assistant.set_qobuz_logged_in(True)

    async def status(self) -> dict[str, Any]:
        return await _ok(await self.client.get(STATUS))

    async def connect(self) -> dict[str, Any]:
        """The user enters their Music Assistant login and presses Connect."""
        return await _ok(await self.client.post(CONNECT, json=_login_form(self.server)))

    async def choose_players(self, **body: Any) -> dict[str, Any]:
        return await _ok(await self.client.put(PLAYERS, json=body))

    async def set_up(self) -> None:
        """Qobuz logged in and Music Assistant connected, so every player is a speaker."""
        await self.log_in_to_qobuz()
        await self.connect()

    def saved(self) -> settings.Settings:
        return settings.load(self.settings_path)


async def _ok(response: ClientResponse) -> dict[str, Any]:
    assert response.status == 200, await response.text()
    result: dict[str, Any] = await response.json()
    return result


def _login_form(server: FakeMusicAssistantServer) -> dict[str, str]:
    return {"url": server.url, "username": "admin", "password": ADMIN_PASSWORD}


def _levels(status: dict[str, Any]) -> str:
    """The checks as "id:level ...", in the order the panel lists them."""
    return " ".join(f"{check['id']}:{check['level']}" for check in status["checks"])


def _detail(status: dict[str, Any], check_id: str) -> str:
    return str(next(check["detail"] for check in status["checks"] if check["id"] == check_id))


def _shown(status: dict[str, Any]) -> dict[str, str]:
    """The players in the Qobuz app, with the names they show there."""
    return {p["player_id"]: p["speaker_name"] for p in status["players"] if p["speaker_name"]}


@pytest.fixture
async def panel(
    server: FakeMusicAssistantServer, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, fake_login
) -> AsyncIterator[Panel]:
    # The real one asks the OS which interface reaches the internet
    monkeypatch.setattr(music_assistant_routes, "_lan_ip", lambda: "192.168.1.10")
    server.add_player("up2", "Living Room")
    panel = Panel(server, tmp_path / "music_assistant.json")
    await panel.music_assistant.start()
    await panel.client.start_server()
    try:
        yield panel
    finally:
        await panel.client.close()
        await panel.music_assistant.stop()
        await server.settle()


async def test_the_page_loads_its_assets_and_has_every_element_its_script_uses(panel):
    page = await (await panel.client.get("/")).text()

    assets = re.findall(r'(?:src|href)="(static/[^"]+)"', page)
    assert {"static/panel.css?v=9.9.9", "static/panel.js?v=9.9.9"} <= set(assets)
    for asset in assets:
        assert (await panel.client.get(f"/{asset}")).status == 200, asset
    assert "{{" not in page

    response = await panel.client.get("/static/panel.js")
    assert response.headers["Cache-Control"] == "no-cache"  # so an update reaches the browser
    script = await response.text()
    ids = set(re.findall(r'\$\("([\w-]+)"\)', script))
    assert ids
    assert [i for i in sorted(ids) if f'id="{i}"' not in page] == []


async def test_the_script_calls_routes_that_exist_as_json_and_by_relative_urls(panel):
    headers = {"X-Ingress-Path": "/api/hassio_ingress/abc123"}
    page = await (await panel.client.get("/", headers=headers)).text()
    assert '<base href="/api/hassio_ingress/abc123/">' in page

    script = await (await panel.client.get("/static/panel.js")).text()
    calls = re.findall(r'(?:request|send)\("([A-Z]+)", "([^"]+)"', script)
    assert calls and all(url.startswith("api/") for _, url in calls), calls
    # A path from the root would step out of the ingress prefix (one added to the
    # add-on's own address, for the framed Qobuz login, is fine)
    assert re.findall(r"""(?<!\+ )["'`]/\w[^"'`]*""", script) == []
    # The guard takes changes only as JSON
    assert re.search(r'headers\["Content-Type"\] = "application/json"', script)

    for method, url in calls:
        response = await panel.client.request(method, f"/{url}", json={})
        assert response.status not in (404, 405), (method, url)


def test_the_qobuz_login_link_uses_an_address_of_this_machine():
    address = music_assistant_routes._lan_ip()
    if not address:
        pytest.skip("this machine has no network route")
    with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as probe:
        probe.bind((address, 0))  # refused for an address that is not this machine's


async def test_a_failed_qobuz_login_shows_an_error_in_the_panel(panel):
    failed = await panel.client.get("/auth/callback", allow_redirects=False)
    assert failed.headers["Location"].startswith("/?error=")

    # No browser here: the page has the message box and the script reads the error parameter
    page = await (await panel.client.get("/")).text()
    script = await (await panel.client.get("/static/panel.js")).text()
    assert 'id="qobuz-error"' in page
    assert 'URLSearchParams(window.location.search).get("error")' in script


async def test_the_checks_lead_the_user_through_setup_in_order(panel, server):
    status = await panel.status()
    assert _levels(status) == "qobuz:error music_assistant:error speakers:pending"
    assert "Log in to Qobuz" in _detail(status, "qobuz")
    # The Qobuz login can't run through Home Assistant, so the panel links the add-on's own port
    assert status["login_url"] == "http://192.168.1.10:8689"

    await panel.log_in_to_qobuz()
    status = await panel.status()
    assert _levels(status) == "qobuz:ok music_assistant:error speakers:pending"
    assert status["qobuz"] == {"logged_in": True, "account": "alice@example.com"}
    assert _detail(status, "qobuz") == "Logged in to Qobuz as alice@example.com."
    assert "Enter your Music Assistant username and password" in _detail(status, "music_assistant")

    # While Music Assistant is still sending its players, the check waits rather than alarms
    server.gate("players/all")
    connecting = asyncio.create_task(panel.connect())
    await wait_for(lambda: server.calls("players/all"), "the connection to load the players")
    status = await panel.status()
    assert _levels(status) == "qobuz:ok music_assistant:pending speakers:pending"
    server.release("players/all")

    status = await connecting
    assert _levels(status) == "qobuz:ok music_assistant:ok qobuz_in_music_assistant:ok speakers:ok"
    version = server.server_version
    assert _detail(status, "music_assistant") == f"Connected to Music Assistant {version}."
    assert _detail(status, "speakers") == "2 speakers and groups are available in the Qobuz app."

    status = await panel.choose_players(expose_all=False)  # and nothing ticked
    assert _levels(status).endswith(" speakers:warn")
    assert "No speakers are shared with the Qobuz app" in _detail(status, "speakers")

    # Logged out of Qobuz, the speakers row points back at the Qobuz login
    await _ok(await panel.client.post("/api/auth/logout", json={}))
    status = await panel.status()
    assert _levels(status).endswith(" speakers:pending")
    assert "once Qobuz and Music Assistant are both connected" in _detail(status, "speakers")


async def test_the_checks_say_what_to_fix_in_music_assistant(panel, server):
    server.add_qobuz_provider(available=False)  # its Qobuz provider failed to load
    await panel.set_up()
    status = await panel.status()
    assert "qobuz_in_music_assistant:error" in _levels(status)
    assert "add Qobuz" in _detail(status, "qobuz_in_music_assistant")

    server.revoke_token(panel.saved().token)
    await wait_for(
        lambda: panel.music_assistant.status()["connection"]["state"] == "auth_failed",
        "Music Assistant to refuse the saved login",
        server=server,
    )
    status = await panel.status()
    assert _levels(status) == "qobuz:ok music_assistant:error speakers:ok"
    assert "Connect again in this panel" in _detail(status, "music_assistant")


async def test_connecting_keeps_the_login_secret_and_disconnecting_forgets_it(panel, server):
    await panel.log_in_to_qobuz()
    response = await panel.client.post(CONNECT, json=_login_form(server))
    answer = await response.text()
    assert response.status == 200
    assert json.loads(answer)["speaker_count"] == 2
    token = panel.saved().token
    assert token in server.valid_tokens
    for text in (answer, await (await panel.client.get(STATUS)).text()):
        assert token not in text and ADMIN_PASSWORD not in text

    response = await panel.client.post(DISCONNECT, json={})
    assert response.status == 200
    status = await response.json()
    assert not status["is_configured"] and status["speaker_count"] == 0
    assert panel.saved().token == ""


async def test_a_wrong_password_is_refused_with_a_plain_message(panel, server):
    response = await panel.client.post(CONNECT, json={**_login_form(server), "password": "nope"})

    assert response.status == 400
    error = (await response.json())["error"]
    assert error.startswith("Music Assistant did not accept that username and password.")
    assert panel.saved().token == ""


async def test_a_token_made_in_music_assistant_connects_without_a_password(panel, server):
    response = await panel.client.post(CONNECT, json={"url": server.url, "token": "not-a-token"})
    assert response.status == 400
    assert "did not accept that token" in (await response.json())["error"]
    assert panel.saved().token == ""

    pasted = {"url": server.url, "token": f" {server.token} "}  # with the spaces a paste brings
    status = await _ok(await panel.client.post(CONNECT, json=pasted))

    assert status["connection"]["state"] == "connected"
    assert status["connection"]["username"] == "admin"
    assert panel.saved().token == server.token  # used as it is: no token of our own is made
    assert server.calls("auth/token/create") == []
    assert server.token not in json.dumps(status)


async def test_a_partial_player_choice_is_applied_saved_and_keeps_the_other_setting(panel):
    await panel.set_up()

    # Ticking a player while "All my Music Assistant players" is on only remembers the tick
    status = await panel.choose_players(selected=["up2"])
    assert status["expose_all"] is True and status["speaker_count"] == 2

    # Switching "all" off then leaves just the ticked player in the Qobuz app
    status = await panel.choose_players(expose_all=False)
    assert _shown(status) == {"up2": "Living Room (MA)"}
    assert status["speaker_count"] == 1

    # From then on a tick swaps the speaker, and "all" stays off
    status = await panel.choose_players(selected=[KITCHEN])
    assert status["expose_all"] is False
    assert _shown(status) == {KITCHEN: "Kitchen (MA)"}
    assert status["speaker_count"] == 1
    saved = panel.saved()
    assert (saved.expose_all, saved.selected_player_ids) == (False, [KITCHEN])


async def test_a_speaker_is_renamed_in_the_panel_and_an_empty_name_brings_the_default_back(
    panel,
):
    await panel.set_up()

    status = await _ok(
        await panel.client.put(SPEAKER_NAME, json={"player_id": KITCHEN, "name": "Küche"})
    )
    kitchen = next(p for p in status["players"] if p["player_id"] == KITCHEN)
    assert (kitchen["speaker_name"], kitchen["custom_name"]) == ("Küche", "Küche")
    assert kitchen["default_name"] == "Kitchen (MA)"  # what an empty name brings back

    taken = await panel.client.put(SPEAKER_NAME, json={"player_id": "up2", "name": "Küche"})
    assert taken.status == 400
    assert "already called" in (await taken.json())["error"]

    status = await _ok(
        await panel.client.put(SPEAKER_NAME, json={"player_id": KITCHEN, "name": ""})
    )
    kitchen = next(p for p in status["players"] if p["player_id"] == KITCHEN)
    assert (kitchen["speaker_name"], kitchen["custom_name"]) == ("Kitchen (MA)", "")


async def test_malformed_requests_are_refused_and_change_nothing(panel, server):
    await panel.set_up()
    before = panel.settings_path.read_text()
    good = _login_form(server)
    cases: list[tuple[str, str, Any, str]] = [
        ("POST", CONNECT, "{not json", "Invalid request."),
        ("PUT", PLAYERS, "[1, 2]", "Invalid request."),
        ("POST", CONNECT, {**good, "remember": True}, "Invalid field: remember"),
        ("POST", CONNECT, {"url": good["url"], "token": 123}, "Invalid field: token"),
        ("POST", CONNECT, {**good, "password": 123}, "Invalid field: password"),
        ("PUT", PLAYERS, {"expose_all": "no"}, "Invalid field: expose_all"),
        ("PUT", PLAYERS, {"selected": [1, 2]}, "Invalid field: selected"),
        ("PUT", SPEAKER_NAME, {"name": "No player"}, "Invalid request."),
        (
            "PUT",
            SPEAKER_NAME,
            {"player_id": "gone", "name": "X"},
            "That Music Assistant player is not known (any more).",
        ),
        # Both would succeed without the length limit
        ("POST", CONNECT, {**good, "url": good["url"] + "/" * 1000}, "Invalid field: url"),
        ("PUT", PLAYERS, {"selected": ["up2"] * 1001}, "Invalid field: selected"),
    ]
    for method, path, body, error in cases:
        data = body if isinstance(body, str) else json.dumps(body)
        response = await panel.client.request(
            method, path, data=data, headers={"Content-Type": "application/json"}
        )
        assert (response.status, await response.json()) == (400, {"error": error}), body

    assert panel.settings_path.read_text() == before


async def test_other_sites_can_neither_change_settings_nor_frame_the_panel(panel):
    await panel.set_up()

    # Another add-on's page on the same host is "same-site", and just as untrusted
    for site in ("cross-site", "same-site"):
        response = await panel.client.post(DISCONNECT, json={}, headers={"Sec-Fetch-Site": site})
        assert response.status == 403, site
    assert panel.saved().token

    # An HTML form on another site can post here, but never as JSON
    response = await panel.client.post("/api/auth/logout", data={"a": "1"})
    assert response.status == 415
    assert panel.logouts == 0
    response = await panel.client.post("/api/auth/logout", json={})  # the panel's Log out button
    assert (response.status, await response.json()) == (200, {"status": "ok"})
    assert panel.logouts == 1

    response = await panel.client.get("/")
    assert "frame-ancestors 'self'" in response.headers["Content-Security-Policy"]
