"""The panel: its static files, the /api/music-assistant/* routes and the plain-language status checks.

The routes run on a real MusicAssistantSpeakers against the in-memory FakeMusicAssistantServer; the
speakers are stubs.
"""

from __future__ import annotations

import asyncio
import json
import re
from pathlib import Path
from typing import Any, Callable, Optional
from unittest.mock import AsyncMock

import pytest

pytest.importorskip("music_assistant_models")
pytest.importorskip("music_assistant_client")

from aiohttp import web  # noqa: E402
from aiohttp.test_utils import TestClient, TestServer  # noqa: E402
from music_assistant_models.enums import PlayerType  # noqa: E402

from qobuz_proxy.backends.music_assistant import connection as connection_module  # noqa: E402
from qobuz_proxy.backends.music_assistant import speakers as manager_module  # noqa: E402
from qobuz_proxy.backends.music_assistant import settings  # noqa: E402
from qobuz_proxy.backends.music_assistant.speakers import MusicAssistantSpeakers  # noqa: E402
from qobuz_proxy.backends.music_assistant.settings import DEFAULT_URL, Settings  # noqa: E402
from qobuz_proxy.webui import music_assistant_routes  # noqa: E402
from qobuz_proxy.webui.music_assistant_routes import build_checks  # noqa: E402
from qobuz_proxy.webui.routes import register_routes  # noqa: E402
from tests.backends.fake_music_assistant import FakeMusicAssistantServer  # noqa: E402

STATIC = Path(__file__).resolve().parents[2] / "qobuz_proxy" / "webui" / "static"
LOGGED_IN = {"authenticated": True, "user_id": "1", "email": "user@example.com", "name": "Alice"}
LOGIN = {"url": "http://127.0.0.1:8095", "username": "admin", "password": "admin-password"}
ALL_OK = {
    "qobuz": "ok",
    "music_assistant": "ok",
    "qobuz_in_music_assistant": "ok",
    "speakers": "ok",
}
QOBUZ_IN = {"logged_in": True, "account": "Alice"}
QOBUZ_OUT = {"logged_in": False, "account": ""}


@pytest.fixture(autouse=True)
def fast_timers(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(manager_module, "UPDATE_DELAY_SECONDS", 0.01)
    monkeypatch.setattr(connection_module, "RECONNECT_DELAYS_SECONDS", (0.02,))


class StubSpeaker:
    def __init__(self, config: Any, backend: Any) -> None:
        pass

    async def start(self) -> bool:
        return True

    async def stop(self) -> None:
        pass

    def get_status(self) -> dict[str, Any]:
        return {"status": "idle"}


class Panel:
    """A real MusicAssistantSpeakers on FakeMusicAssistantServer behind the real web routes."""

    def __init__(self, server: FakeMusicAssistantServer, path: Path) -> None:
        self.server = server
        self.path = path
        self.lan_ip = "192.168.1.10"
        self.auth_state = dict(LOGGED_IN)
        self.music_assistant = MusicAssistantSpeakers(
            path,
            StubSpeaker,
            client_factory=server.client_factory,
            port_is_free=lambda address, port: True,
        )
        self.client: Optional[TestClient] = None

    def saved(self) -> Settings:
        return settings.load(self.path)

    def app(self) -> web.Application:
        app = web.Application()
        app["auth_state"] = self.auth_state
        app["get_speakers"] = lambda: []
        app["version"] = "9.9.9"
        app["on_auth_token"] = AsyncMock(return_value=True)
        app["on_logout"] = AsyncMock()
        app["http_port"] = 8689
        app["music_assistant"] = self.music_assistant
        register_routes(app)
        return app

    async def wait_for(self, cond: Callable[[], Any], what: str, timeout: float = 3.0) -> None:
        loop = asyncio.get_running_loop()
        deadline = loop.time() + timeout
        while True:
            await self.server.settle()
            if cond():
                return
            if loop.time() > deadline:
                pytest.fail(f"timed out waiting for {what}")
            await asyncio.sleep(0.005)

    async def status(self) -> dict[str, Any]:
        assert self.client is not None
        resp = await self.client.get("/api/music-assistant/status")
        assert resp.status == 200
        result: dict[str, Any] = await resp.json()
        return result


@pytest.fixture
def server(monkeypatch: pytest.MonkeyPatch) -> FakeMusicAssistantServer:
    server = FakeMusicAssistantServer()
    server.add_qobuz_provider()
    server.add_player("up1", "Kitchen")
    server.add_player("grp", "Downstairs", type=PlayerType.GROUP, group_members=["up1"])
    monkeypatch.setattr(connection_module, "login", server.login)
    monkeypatch.setattr(connection_module, "MusicAssistantClient", server.client_class)
    return server


async def open_panel(
    server: FakeMusicAssistantServer,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    *,
    configured: bool,
) -> Panel:
    path = tmp_path / "music_assistant.json"
    if configured:
        settings.save(path, Settings(url=server.url, token=server.token, username="admin"))
    panel = Panel(server, path)
    monkeypatch.setattr(music_assistant_routes, "_lan_ip", lambda: panel.lan_ip)
    await panel.music_assistant.start()
    await panel.music_assistant.set_qobuz_logged_in(True)
    if configured:
        await panel.wait_for(
            lambda: panel.music_assistant.status()["speaker_count"] == 2, "devices"
        )
    panel.client = TestClient(TestServer(panel.app()))
    await panel.client.start_server()
    return panel


@pytest.fixture
async def configured(server, tmp_path, monkeypatch):
    panel = await open_panel(server, tmp_path, monkeypatch, configured=True)
    try:
        yield panel
    finally:
        await panel.client.close()
        await panel.music_assistant.stop()


@pytest.fixture
async def fresh(server, tmp_path, monkeypatch):
    panel = await open_panel(server, tmp_path, monkeypatch, configured=False)
    try:
        yield panel
    finally:
        await panel.client.close()
        await panel.music_assistant.stop()


def checks_by_id(checks: list[dict[str, str]]) -> dict[str, dict[str, str]]:
    return {c["id"]: c for c in checks}


def levels(checks: list[dict[str, str]]) -> list[tuple[str, str]]:
    return [(c["id"], c["level"]) for c in checks]


async def test_index_loads_the_panel_assets_with_the_cache_buster(fresh):
    body = await (await fresh.client.get("/")).text()
    assert 'src="static/panel.js?v=9.9.9"' in body
    assert 'href="static/panel.css?v=9.9.9"' in body
    assert "{{" not in body and "}}" not in body


def test_every_element_id_panel_js_looks_up_exists_in_index():
    html = (STATIC / "index.html").read_text()
    ids = set(re.findall(r"""\$\("([\w-]+)"\)""", (STATIC / "panel.js").read_text()))
    assert ids
    assert sorted(i for i in ids if f'id="{i}"' not in html) == []


def test_panel_js_only_uses_relative_api_urls():
    js = (STATIC / "panel.js").read_text()
    urls = re.findall(r"""(?:request|send)\("[A-Z]+", "([^"]+)\"""", js)
    assert urls and all(url.startswith("api/") for url in urls), urls
    # An absolute path would escape the Home Assistant ingress prefix
    assert not re.search(r"""["'`]/api/""", js)


class TestStatusRoute:
    async def test_status_has_checks_and_the_login_url_but_never_the_token(
        self, configured, server
    ):
        resp = await configured.client.get("/api/music-assistant/status")
        text = await resp.text()
        body = json.loads(text)

        assert server.token not in text and "token" not in body
        assert body["login_url"] == "http://192.168.1.10:8689"
        assert body["qobuz"] == QOBUZ_IN
        assert levels(body["checks"]) == list(ALL_OK.items())
        assert body["connection"]["state"] == "connected"
        assert body["speaker_count"] == 2
        assert {p["player_id"] for p in body["players"]} == {"up1", "grp"}

    async def test_login_url_is_empty_without_a_lan_address(self, configured):
        configured.lan_ip = ""
        assert (await configured.status())["login_url"] == ""

    async def test_qobuz_account_falls_back_to_the_email(self, configured):
        configured.auth_state["name"] = ""
        qobuz = (await configured.status())["qobuz"]
        assert qobuz == {"logged_in": True, "account": "user@example.com"}


def panel_status(
    state: str = "connected",
    *,
    message: str = "",
    configured: bool = True,
    has_qobuz_provider: bool = True,
    speaker_count: int = 1,
    speaker_names: Optional[list[str]] = None,
) -> dict[str, Any]:
    if speaker_names is None:
        speaker_names = ["Kitchen (MA)"] * speaker_count
    return {
        "is_configured": configured,
        "connection": {
            "state": state,
            "message": message,
            "server_version": "2.10.7",
            "has_qobuz_provider": has_qobuz_provider,
        },
        "speaker_count": speaker_count,
        "players": [{"speaker_name": name} for name in speaker_names],
    }


class TestBuildChecks:
    @pytest.mark.parametrize(
        ("qobuz", "status", "expected"),
        [
            pytest.param(QOBUZ_IN, panel_status(), ALL_OK, id="all_good"),
            pytest.param(
                QOBUZ_OUT,
                panel_status(speaker_count=0),
                {
                    "qobuz": "error",
                    "music_assistant": "ok",
                    "qobuz_in_music_assistant": "ok",
                    "speakers": "pending",
                },
                id="qobuz_logged_out",
            ),
            pytest.param(
                QOBUZ_IN,
                panel_status("not_configured", configured=False, speaker_count=0),
                {"qobuz": "ok", "music_assistant": "error", "speakers": "pending"},
                id="music_assistant_not_configured",
            ),
            pytest.param(
                QOBUZ_IN,
                panel_status("connecting", speaker_count=0),
                {"qobuz": "ok", "music_assistant": "pending", "speakers": "pending"},
                id="connecting",
            ),
            pytest.param(
                QOBUZ_IN,
                panel_status("disconnected", message="Lost it", speaker_count=2),
                {"qobuz": "ok", "music_assistant": "error", "speakers": "ok"},
                id="speakers_count_while_music_assistant_is_down",
            ),
            pytest.param(
                QOBUZ_IN,
                panel_status("auth_failed", message="Log in again", speaker_count=0),
                {"qobuz": "ok", "music_assistant": "error", "speakers": "pending"},
                id="auth_failed",
            ),
            pytest.param(
                QOBUZ_IN,
                panel_status(has_qobuz_provider=False),
                {
                    "qobuz": "ok",
                    "music_assistant": "ok",
                    "qobuz_in_music_assistant": "error",
                    "speakers": "ok",
                },
                id="no_qobuz_provider_in_ma",
            ),
            pytest.param(
                QOBUZ_IN,
                panel_status(speaker_count=0, speaker_names=[""]),
                {
                    "qobuz": "ok",
                    "music_assistant": "ok",
                    "qobuz_in_music_assistant": "ok",
                    "speakers": "warn",
                },
                id="nothing_shared",
            ),
            pytest.param(
                QOBUZ_IN,
                panel_status(speaker_count=0, speaker_names=["Kitchen (MA)"]),
                {
                    "qobuz": "ok",
                    "music_assistant": "ok",
                    "qobuz_in_music_assistant": "ok",
                    "speakers": "pending",
                },
                id="devices_starting",
            ),
        ],
    )
    def test_checks_come_in_the_order_the_user_fixes_them_with_their_levels(
        self, qobuz, status, expected
    ):
        assert levels(build_checks(qobuz, status)) == list(expected.items())

    @pytest.mark.parametrize(
        ("state", "configured"),
        [("disconnected", True), ("auth_failed", True), ("not_configured", False)],
    )
    def test_ma_check_shows_why_music_assistant_is_not_connected(self, state, configured):
        message = "Can't reach Music Assistant at http://ma:8095."
        status = panel_status(state, message=message, configured=configured, speaker_count=0)
        ma = checks_by_id(build_checks(QOBUZ_IN, status))["music_assistant"]
        assert (ma["level"], ma["detail"]) == ("error", message)

    def test_details_name_the_qobuz_account_and_the_ma_version(self):
        checks = build_checks(QOBUZ_IN, panel_status())
        assert all(set(c) == {"id", "level", "title", "detail"} for c in checks)
        assert "Alice" in checks_by_id(checks)["qobuz"]["detail"]
        assert "2.10.7" in checks_by_id(checks)["music_assistant"]["detail"]


class TestConnectRoute:
    async def test_login_connects_and_returns_the_status_without_secrets(self, fresh):
        before = await fresh.status()
        assert before["is_configured"] is False
        assert before["connection"]["state"] == "not_configured"
        assert before["connection"]["url"] == DEFAULT_URL

        resp = await fresh.client.post("/api/music-assistant/connect", json=LOGIN)

        assert resp.status == 200
        text = await resp.text()
        body = json.loads(text)
        assert body["is_configured"] is True
        assert body["connection"]["state"] == "connected"
        assert levels(body["checks"]) == list(ALL_OK.items())
        token = fresh.saved().token
        assert token and token not in text
        assert LOGIN["password"] not in text

    async def test_wrong_password_is_a_400_with_a_plain_message(self, fresh):
        resp = await fresh.client.post(
            "/api/music-assistant/connect", json={**LOGIN, "password": "nope"}
        )
        assert resp.status == 400
        assert "did not accept that username and password" in (await resp.json())["error"]
        assert fresh.saved().token == ""

    async def test_disconnect_forgets_the_login(self, configured):
        resp = await configured.client.post("/api/music-assistant/disconnect")
        assert resp.status == 200
        body = await resp.json()
        assert body["is_configured"] is False and body["speaker_count"] == 0
        assert configured.saved().token == ""


class TestPlayersRoute:
    async def test_update_is_applied_saved_and_returned(self, configured):
        resp = await configured.client.put(
            "/api/music-assistant/players", json={"expose_all": False, "selected": ["grp"]}
        )
        assert resp.status == 200
        status = await resp.json()
        assert status["expose_all"] is False
        players = {p["player_id"]: p for p in status["players"]}
        assert players["grp"]["selected"] and players["grp"]["speaker_name"] == "Downstairs (MA)"
        assert players["up1"]["speaker_name"] == ""
        assert status["speaker_count"] == 1
        saved = configured.saved()
        assert saved.expose_all is False and saved.selected_player_ids == ["grp"]

    async def test_partial_update_keeps_the_other_settings(self, configured):
        resp = await configured.client.put(
            "/api/music-assistant/players", json={"selected": ["grp"]}
        )
        assert resp.status == 200
        assert (await resp.json())["speaker_count"] == 2  # still all players
        saved = configured.saved()
        assert saved.expose_all is True and saved.selected_player_ids == ["grp"]


WRITES = [("post", "/api/music-assistant/connect"), ("put", "/api/music-assistant/players")]


class TestRequestValidation:
    @pytest.mark.parametrize("payload", ["{not json", "[1, 2]", '"text"', ""])
    @pytest.mark.parametrize(("method", "path"), WRITES)
    async def test_a_body_that_is_not_a_json_object_is_a_400(
        self, configured, method, path, payload
    ):
        resp = await configured.client.request(
            method, path, data=payload, headers={"Content-Type": "application/json"}
        )
        assert resp.status == 400
        assert await resp.json() == {"error": "Invalid request."}

    @pytest.mark.parametrize(
        ("method", "path", "payload"),
        [
            ("post", "/api/music-assistant/connect", {**LOGIN, "token": "pasted-token"}),
            ("post", "/api/music-assistant/connect", {**LOGIN, "password": 123}),
            ("put", "/api/music-assistant/players", {"name_suffix": False}),
            ("put", "/api/music-assistant/players", {"expose_all": "no"}),
            ("put", "/api/music-assistant/players", {"expose_all": 1}),
            ("put", "/api/music-assistant/players", {"selected": "up1"}),
            ("put", "/api/music-assistant/players", {"selected": [1, 2]}),
            ("put", "/api/music-assistant/players", {"selected": {"up1": True}}),
        ],
    )
    async def test_unknown_fields_and_wrong_types_are_a_400_and_change_nothing(
        self, configured, method, path, payload
    ):
        before = configured.saved()
        resp = await configured.client.request(method, path, json=payload)
        assert resp.status == 400
        assert (await resp.json())["error"]
        assert configured.saved() == before
