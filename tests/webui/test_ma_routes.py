"""Web UI routes for Music Assistant (/api/ma/*) and the plain-language status checks."""

from __future__ import annotations

import asyncio
import json
from typing import Any, Callable, Optional
from unittest.mock import AsyncMock

import pytest

pytest.importorskip("music_assistant_models")
pytest.importorskip("music_assistant_client")

from aiohttp import web  # noqa: E402
from aiohttp.test_utils import TestClient, TestServer  # noqa: E402
from music_assistant_models.enums import PlayerType  # noqa: E402

from qobuz_proxy.backends.music_assistant import connection as connection_module  # noqa: E402
from qobuz_proxy.backends.music_assistant import manager as manager_module  # noqa: E402
from qobuz_proxy.backends.music_assistant.connection import MusicAssistantConnection  # noqa: E402
from qobuz_proxy.backends.music_assistant.manager import MusicAssistantManager  # noqa: E402
from qobuz_proxy.backends.music_assistant.settings import MASettings, MASettingsStore  # noqa: E402
from qobuz_proxy.config import SpeakerConfig  # noqa: E402
from qobuz_proxy.webui import ma_routes  # noqa: E402
from qobuz_proxy.webui.ma_routes import build_checks  # noqa: E402
from qobuz_proxy.webui.routes import register_routes  # noqa: E402
from tests.backends.fake_music_assistant import FakeMAServer  # noqa: E402

LOGGED_IN = {"authenticated": True, "user_id": "1", "email": "user@example.com", "name": "Alice"}
LOGGED_OUT = {"authenticated": False, "user_id": "", "email": ""}
CHECK_IDS = ["qobuz", "ma", "ma_qobuz", "exposed"]


@pytest.fixture(autouse=True)
def fast_timers(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(manager_module, "RECONCILE_DELAY_SECONDS", 0.01)
    monkeypatch.setattr(MusicAssistantConnection, "RECONNECT_DELAYS_SECONDS", (0.02,))


class StubSpeaker:
    def __init__(self, config: SpeakerConfig, connection: Any) -> None:
        self.config = config

    async def start(self) -> bool:
        return True

    async def stop(self) -> None:
        pass

    def get_status(self) -> dict[str, Any]:
        return {"status": "idle", "now_playing": None}


class Panel:
    """A real MusicAssistantManager on FakeMAServer behind the real web routes."""

    def __init__(self, server: FakeMAServer, store: MASettingsStore) -> None:
        self.server = server
        self.store = store
        self.lan_ip_lookups = 0
        self.lan_ip = "192.168.1.10"
        self.manager = MusicAssistantManager(
            store=store,
            speaker_factory=StubSpeaker,
            reserved_ports=lambda: {8689},
            yaml_speakers=lambda: [],
            connection_factory=lambda url, token: MusicAssistantConnection(
                url, token, client_factory=server.client_factory
            ),
            url_detector=self._detect,
            port_checker=lambda address, port: True,
        )
        self.client: Optional[TestClient] = None

    async def _detect(self) -> str:
        return "http://192.168.1.20:8095"

    def direct_lan_ip(self) -> str:
        self.lan_ip_lookups += 1
        return self.lan_ip

    def app(self, auth_state: dict[str, Any], *, with_manager: bool = True) -> web.Application:
        app = web.Application()
        app["auth_state"] = auth_state
        app["get_speakers"] = lambda: []
        app["version"] = "test"
        app["on_auth_token"] = AsyncMock(return_value=True)
        app["on_logout"] = AsyncMock()
        app["http_port"] = 8689
        if with_manager:
            app["ma_manager"] = self.manager
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


@pytest.fixture
def server(monkeypatch: pytest.MonkeyPatch) -> FakeMAServer:
    server = FakeMAServer()
    server.add_qobuz_provider()
    server.add_player("up1", "Kitchen")
    server.add_player("grp", "Downstairs", type=PlayerType.GROUP, group_members=["up1"])
    monkeypatch.setattr(connection_module, "login", server.login)
    monkeypatch.setattr(connection_module, "MusicAssistantClient", server.client_class)
    return server


async def open_panel(
    server: FakeMAServer,
    tmp_path: Any,
    monkeypatch: pytest.MonkeyPatch,
    *,
    configured: bool,
    auth_state: dict[str, Any] = LOGGED_IN,
    with_manager: bool = True,
) -> Panel:
    store = MASettingsStore(tmp_path / "music_assistant.json")
    if configured:
        store.save(MASettings(url=server.url, token=server.token, username="admin"))
    panel = Panel(server, store)
    monkeypatch.setattr(ma_routes, "direct_lan_ip", panel.direct_lan_ip)
    await panel.manager.start()
    await panel.manager.set_qobuz_ready(bool(auth_state.get("authenticated")))
    if configured:
        await panel.wait_for(lambda: panel.manager.status()["exposed_count"] == 2, "devices")
    panel.client = TestClient(TestServer(panel.app(auth_state, with_manager=with_manager)))
    await panel.client.start_server()
    return panel


@pytest.fixture
async def configured(server, tmp_path, monkeypatch):
    panel = await open_panel(server, tmp_path, monkeypatch, configured=True)
    try:
        yield panel
    finally:
        await panel.client.close()
        await panel.manager.stop()


@pytest.fixture
async def fresh(server, tmp_path, monkeypatch):
    panel = await open_panel(server, tmp_path, monkeypatch, configured=False)
    try:
        yield panel
    finally:
        await panel.client.close()
        await panel.manager.stop()


@pytest.fixture
async def no_manager(server, tmp_path, monkeypatch):
    panel = await open_panel(server, tmp_path, monkeypatch, configured=False, with_manager=False)
    try:
        yield panel
    finally:
        await panel.client.close()
        await panel.manager.stop()


def checks_by_id(checks: list[dict[str, str]]) -> dict[str, dict[str, str]]:
    return {c["id"]: c for c in checks}


# =============================================================================
# GET /api/ma/status
# =============================================================================


class TestStatusRoute:
    async def test_status_has_checks_and_the_direct_login_url_but_never_the_token(
        self, configured, server
    ):
        resp = await configured.client.get("/api/ma/status")
        assert resp.status == 200
        text = await resp.text()
        body = json.loads(text)

        assert server.token not in text
        assert "token" not in body
        assert body["direct_login_url"] == "http://192.168.1.10:8689"
        assert [c["id"] for c in body["checks"]] == CHECK_IDS
        assert {c["id"]: c["level"] for c in body["checks"]} == {
            "qobuz": "ok",
            "ma": "ok",
            "ma_qobuz": "ok",
            "exposed": "ok",
        }
        assert body["connection"]["state"] == "connected"
        assert body["exposed_count"] == 2
        assert {p["player_id"] for p in body["players"]} == {"up1", "grp"}

    async def test_direct_login_url_is_empty_without_a_lan_address(self, configured):
        configured.lan_ip = ""
        body = await (await configured.client.get("/api/ma/status")).json()
        assert body["direct_login_url"] == ""

    async def test_lan_address_is_not_looked_up_on_every_poll(self, configured):
        for _ in range(3):
            assert (await configured.client.get("/api/ma/status")).status == 200
        assert configured.lan_ip_lookups == 1

    async def test_status_before_connecting_suggests_an_address(self, fresh):
        body = await (await fresh.client.get("/api/ma/status")).json()
        assert body["configured"] is False
        assert body["connection"]["state"] == "not_configured"
        assert checks_by_id(body["checks"])["ma"]["level"] == "error"
        await fresh.wait_for(lambda: fresh.manager.status()["suggested_url"] != "", "detection")
        body = await (await fresh.client.get("/api/ma/status")).json()
        assert body["suggested_url"] == "http://192.168.1.20:8095"


# =============================================================================
# build_checks()
# =============================================================================


def ma_status(
    state: str = "connected",
    *,
    message: str = "",
    configured: bool = True,
    supported: bool = True,
    qobuz_provider: Optional[bool] = True,
    exposed_count: int = 1,
    players: Optional[list[dict[str, Any]]] = None,
    server_version: str = "2.10.7",
) -> dict[str, Any]:
    return {
        "supported": supported,
        "configured": configured,
        "connection": {
            "state": state,
            "message": message,
            "server_version": server_version,
            "qobuz_provider": qobuz_provider if state == "connected" else None,
        },
        "exposed_count": exposed_count,
        "players": players if players is not None else [{"exposed": True}] * exposed_count,
    }


class TestBuildChecks:
    def test_checks_come_in_the_order_the_user_fixes_them(self):
        assert [c["id"] for c in build_checks(LOGGED_IN, ma_status())] == CHECK_IDS
        for check in build_checks(LOGGED_IN, ma_status()):
            assert set(check) == {"id", "level", "title", "detail"}

    def test_all_good(self):
        checks = checks_by_id(build_checks(LOGGED_IN, ma_status(exposed_count=1)))
        assert checks["qobuz"] == {
            "id": "qobuz",
            "level": "ok",
            "title": "Qobuz account",
            "detail": "Logged in to Qobuz as Alice.",
        }
        assert checks["ma"]["level"] == "ok"
        assert checks["ma"]["detail"] == "Connected to Music Assistant 2.10.7."
        assert checks["ma_qobuz"]["level"] == "ok"
        assert checks["ma_qobuz"]["detail"] == "Qobuz is set up in Music Assistant."
        assert checks["exposed"]["level"] == "ok"
        assert checks["exposed"]["detail"] == "1 speaker or group is available in the Qobuz app."

    def test_qobuz_login_falls_back_to_the_email(self):
        auth = {"authenticated": True, "email": "user@example.com"}
        qobuz = checks_by_id(build_checks(auth, ma_status()))["qobuz"]
        assert qobuz["detail"] == "Logged in to Qobuz as user@example.com."

    def test_not_logged_in_to_qobuz(self):
        checks = checks_by_id(build_checks(LOGGED_OUT, ma_status(exposed_count=0, players=[])))
        assert checks["qobuz"]["level"] == "error"
        assert checks["qobuz"]["detail"].startswith("Not logged in to Qobuz yet.")
        assert "home Wi" in checks["qobuz"]["detail"]
        assert checks["exposed"]["level"] == "pending"
        assert checks["exposed"]["detail"] == (
            "Speakers appear in the Qobuz app once Qobuz and Music Assistant are both connected."
        )

    def test_ma_not_configured(self):
        status = ma_status("not_configured", configured=False, exposed_count=0, players=[])
        checks = checks_by_id(build_checks(LOGGED_IN, status))
        assert checks["ma"]["level"] == "error"
        assert checks["ma"]["detail"] == (
            "Not connected to Music Assistant yet. Enter your Music Assistant username and "
            "password below."
        )
        assert "ma_qobuz" not in checks
        assert checks["exposed"]["level"] == "pending"

    def test_connecting(self):
        status = ma_status("connecting", exposed_count=0, players=[])
        checks = checks_by_id(build_checks(LOGGED_IN, status))
        assert checks["ma"] == {
            "id": "ma",
            "level": "pending",
            "title": "Music Assistant",
            "detail": "Connecting to Music Assistant…",
        }
        assert "ma_qobuz" not in checks

    def test_disconnected_shows_the_connection_message(self):
        message = "Can't reach Music Assistant at http://ma:8095. Is the add-on running?"
        checks = checks_by_id(build_checks(LOGGED_IN, ma_status("disconnected", message=message)))
        assert checks["ma"]["level"] == "error"
        assert checks["ma"]["detail"] == message
        assert "ma_qobuz" not in checks

    def test_disconnected_without_a_message(self):
        checks = checks_by_id(build_checks(LOGGED_IN, ma_status("disconnected")))
        assert checks["ma"]["detail"] == "Can't reach Music Assistant."

    def test_auth_failed_shows_the_connection_message(self):
        message = "Music Assistant no longer accepts the saved login. Connect again in this panel."
        checks = checks_by_id(build_checks(LOGGED_IN, ma_status("auth_failed", message=message)))
        assert checks["ma"] == {
            "id": "ma",
            "level": "error",
            "title": "Music Assistant",
            "detail": message,
        }

    def test_unsupported_build(self):
        status = ma_status("not_configured", supported=False, configured=False, exposed_count=0)
        checks = checks_by_id(build_checks(LOGGED_IN, status))
        assert checks["ma"]["level"] == "error"
        assert checks["ma"]["detail"] == "Music Assistant support is not installed in this build."

    def test_connected_without_a_qobuz_provider(self):
        checks = checks_by_id(build_checks(LOGGED_IN, ma_status(qobuz_provider=False)))
        assert checks["ma"]["level"] == "ok"
        assert checks["ma_qobuz"]["level"] == "error"
        assert "no working Qobuz provider" in checks["ma_qobuz"]["detail"]
        assert "Providers" in checks["ma_qobuz"]["detail"]

    def test_nothing_shared(self):
        status = ma_status(exposed_count=0, players=[{"exposed": False}])
        exposed = checks_by_id(build_checks(LOGGED_IN, status))["exposed"]
        assert exposed["level"] == "warn"
        assert exposed["detail"].startswith("No speakers are shared with the Qobuz app.")

    @pytest.mark.parametrize(
        ("count", "detail"),
        [
            (1, "1 speaker or group is available in the Qobuz app."),
            (2, "2 speakers and groups are available in the Qobuz app."),
            (5, "5 speakers and groups are available in the Qobuz app."),
        ],
    )
    def test_shared_counts(self, count, detail):
        exposed = checks_by_id(build_checks(LOGGED_IN, ma_status(exposed_count=count)))["exposed"]
        assert exposed == {
            "id": "exposed",
            "level": "ok",
            "title": "Speakers in the Qobuz app",
            "detail": detail,
        }

    def test_devices_still_starting(self):
        status = ma_status(exposed_count=0, players=[{"exposed": True}, {"exposed": False}])
        exposed = checks_by_id(build_checks(LOGGED_IN, status))["exposed"]
        assert exposed["level"] == "pending"
        assert exposed["detail"] == "Starting the Qobuz Connect devices…"

    def test_devices_shared_while_ma_is_down_still_count(self):
        status = ma_status("disconnected", message="Lost the connection", exposed_count=2)
        exposed = checks_by_id(build_checks(LOGGED_IN, status))["exposed"]
        assert exposed["level"] == "ok"


# =============================================================================
# POST /api/ma/connect, POST /api/ma/disconnect
# =============================================================================


class TestConnectRoute:
    async def test_login_succeeds_and_returns_the_status_without_the_token(self, fresh, server):
        resp = await fresh.client.post(
            "/api/ma/connect",
            json={"url": server.url, "username": "admin", "password": "admin-password"},
        )
        assert resp.status == 200
        text = await resp.text()
        body = json.loads(text)
        assert body["ok"] is True
        assert body["status"]["configured"] is True
        saved = fresh.store.load()
        assert saved.token and saved.token not in text
        assert "admin-password" not in text
        assert [c["id"] for c in body["status"]["checks"]][:2] == ["qobuz", "ma"]
        await fresh.wait_for(lambda: fresh.manager.connection.is_connected, "MA connected")
        status = await (await fresh.client.get("/api/ma/status")).json()
        assert [c["id"] for c in status["checks"]] == CHECK_IDS

    async def test_status_returned_by_a_successful_login_shows_connected(self, fresh, server):
        resp = await fresh.client.post(
            "/api/ma/connect", json={"url": server.url, "token": server.token}
        )
        body = await resp.json()
        assert body["ok"] is True
        assert body["status"]["connection"]["state"] == "connected"
        assert checks_by_id(body["status"]["checks"])["ma"]["level"] == "ok"

    async def test_wrong_password_is_a_400_with_a_plain_message(self, fresh, server):
        resp = await fresh.client.post(
            "/api/ma/connect", json={"url": server.url, "username": "admin", "password": "nope"}
        )
        assert resp.status == 400
        body = await resp.json()
        assert body["ok"] is False
        assert "did not accept that username and password" in body["error"]
        assert fresh.store.load().token == ""

    async def test_missing_fields_are_a_400(self, fresh, server):
        resp = await fresh.client.post("/api/ma/connect", json={"url": server.url})
        assert resp.status == 400
        body = await resp.json()
        assert body["ok"] is False
        assert "Enter your Music Assistant username and password" in body["error"]

    @pytest.mark.parametrize("payload", ["{not json", "[1, 2]", ""])
    async def test_invalid_json_is_a_400(self, fresh, payload):
        resp = await fresh.client.post(
            "/api/ma/connect", data=payload, headers={"Content-Type": "application/json"}
        )
        assert resp.status == 400
        assert await resp.json() == {"ok": False, "error": "Invalid request."}

    async def test_disconnect_forgets_the_login(self, configured):
        resp = await configured.client.post("/api/ma/disconnect")
        assert resp.status == 200
        assert await resp.json() == {"ok": True}
        assert configured.store.load().token == ""
        body = await (await configured.client.get("/api/ma/status")).json()
        assert body["configured"] is False
        assert body["exposed_count"] == 0


# =============================================================================
# PUT /api/ma/players
# =============================================================================


class TestPlayersRoute:
    @pytest.mark.parametrize(
        "payload",
        [
            {"expose_all": "yes"},
            {"expose_all": 1},
            {"name_suffix": "false"},
            {"name_suffix": 0},
            {"selected": "up1"},
            {"selected": [1, 2]},
            {"selected": {"up1": True}},
        ],
    )
    async def test_wrong_types_are_a_400(self, configured, payload):
        resp = await configured.client.put("/api/ma/players", json=payload)
        assert resp.status == 400
        body = await resp.json()
        assert body["ok"] is False and body["error"]
        assert configured.store.load().expose_all is True  # nothing changed

    async def test_invalid_json_is_a_400(self, configured):
        resp = await configured.client.put(
            "/api/ma/players", data="nope", headers={"Content-Type": "application/json"}
        )
        assert resp.status == 400

    async def test_update_is_applied_saved_and_returned(self, configured):
        resp = await configured.client.put(
            "/api/ma/players",
            json={"expose_all": False, "selected": ["grp"], "name_suffix": False},
        )
        assert resp.status == 200
        body = await resp.json()
        assert body["ok"] is True
        status = body["status"]
        assert status["expose_all"] is False and status["name_suffix"] is False
        players = {p["player_id"]: p for p in status["players"]}
        assert players["grp"]["selected"] and players["grp"]["exposed"]
        assert players["grp"]["qobuz_name"] == "Downstairs"
        assert not players["up1"]["exposed"]
        assert status["exposed_count"] == 1
        saved = configured.store.load()
        assert saved.expose_all is False
        assert saved.selected_player_ids == ["grp"]
        assert saved.name_suffix is False

    async def test_partial_update_keeps_the_other_settings(self, configured):
        await configured.client.put("/api/ma/players", json={"name_suffix": False})
        resp = await configured.client.put("/api/ma/players", json={})
        assert resp.status == 200
        saved = configured.store.load()
        assert saved.name_suffix is False and saved.expose_all is True


# =============================================================================
# Without a manager (e.g. MA support missing at start-up)
# =============================================================================


class TestWithoutManager:
    @pytest.mark.parametrize(
        ("method", "path"),
        [
            ("get", "/api/ma/status"),
            ("post", "/api/ma/connect"),
            ("post", "/api/ma/disconnect"),
            ("put", "/api/ma/players"),
        ],
    )
    async def test_routes_answer_404(self, no_manager, method, path):
        resp = await getattr(no_manager.client, method)(path, json={})
        assert resp.status == 404
        assert await resp.json() == {"error": "music_assistant_unavailable"}
