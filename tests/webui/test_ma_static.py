"""Tests for the Music Assistant panel: static files and the route plumbing they call."""

import json
import re
from pathlib import Path
from typing import Any, Optional
from unittest.mock import AsyncMock, patch

import pytest
from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer

from qobuz_proxy.webui.routes import register_routes

STATIC = Path(__file__).resolve().parents[2] / "qobuz_proxy" / "webui" / "static"
SECRET_TOKEN = "ma-secret-token-123"


class StubManager:
    """Stands in for MusicAssistantManager; holds a token the UI must never see."""

    def __init__(self) -> None:
        self._token = SECRET_TOKEN
        self.connect_result: dict[str, Any] = {"ok": True}
        self.connect = AsyncMock(side_effect=self._connect)
        self.disconnect = AsyncMock()
        self.update_exposure = AsyncMock(return_value=None)

    async def _connect(
        self, url: str, username: str = "", password: str = "", token: str = ""
    ) -> dict[str, Any]:
        if token:
            self._token = token
        return self.connect_result

    def status(self) -> dict[str, Any]:
        return {
            "supported": True,
            "support_error": "",
            "configured": True,
            "connection": {
                "state": "connected",
                "message": "Connected to Music Assistant 2.10.7.",
                "url": "http://127.0.0.1:8095",
                "username": "alex",
                "server_version": "2.10.7",
                "qobuz_provider": True,
            },
            "suggested_url": "http://127.0.0.1:8095",
            "expose_all": True,
            "name_suffix": True,
            "qobuz_ready": True,
            "players": [
                {
                    "player_id": "up_kitchen",
                    "ma_name": "Kitchen",
                    "qobuz_name": "Kitchen (MA)",
                    "type": "player",
                    "available": True,
                    "hidden": False,
                    "selected": False,
                    "exposed": True,
                    "in_yaml": False,
                    "device": {"status": "idle", "now_playing": None},
                }
            ],
            "exposed_count": 1,
        }


def make_app(manager: Optional[Any] = None, authenticated: bool = True) -> web.Application:
    app = web.Application()
    app["auth_state"] = {
        "authenticated": authenticated,
        "user_id": "1" if authenticated else "",
        "email": "alex@example.com" if authenticated else "",
        "name": "Alex" if authenticated else "",
    }
    app["get_speakers"] = lambda: []
    app["version"] = "9.9.9"
    app["http_port"] = 8689
    app["on_auth_token"] = AsyncMock(return_value=True)
    app["on_logout"] = AsyncMock()
    if manager is not None:
        app["ma_manager"] = manager
    register_routes(app)
    return app


@pytest.fixture
def manager() -> StubManager:
    return StubManager()


@pytest.fixture
async def client(manager: StubManager):
    with patch("qobuz_proxy.webui.ma_routes.direct_lan_ip", return_value="192.168.1.20"):
        async with TestClient(TestServer(make_app(manager))) as c:
            yield c


@pytest.fixture
async def bare_client():
    """Plain QobuzProxy: no Music Assistant manager registered."""
    async with TestClient(TestServer(make_app(None))) as c:
        yield c


def _keys(obj: Any) -> set[str]:
    if isinstance(obj, dict):
        return set(obj) | {k for v in obj.values() for k in _keys(v)}
    if isinstance(obj, list):
        return {k for v in obj for k in _keys(v)}
    return set()


# ---------------------------------------------------------------------------
# Static UI
# ---------------------------------------------------------------------------


async def test_index_loads_ma_js_after_app_js(client: TestClient) -> None:
    resp = await client.get("/")
    assert resp.status == 200
    body = await resp.text()
    assert '<script src="static/ma.js?v=9.9.9"></script>' in body
    assert body.index("static/app.js?v=9.9.9") < body.index("static/ma.js?v=9.9.9")
    assert 'window.QOBUZ_DIRECT_PORT = "8689"' in body
    assert "{{" not in body


async def test_index_has_main_sections(client: TestClient) -> None:
    body = await (await client.get("/")).text()
    for section_id in (
        "ma-status-card",
        "ma-checks",
        "qobuz-card",
        "ma-login-hint",
        "ma-card",
        "ma-connect-form",
        "ma-players-card",
        "ma-expose-all",
        "ma-name-suffix",
        "ma-players",
        "other-speakers",
    ):
        assert f'id="{section_id}"' in body, section_id
    # the upstream DLNA/local section sits in a collapsed <details>
    assert '<details id="other-speakers"' in body
    assert '<details id="other-speakers" open' not in body


async def test_index_keeps_the_ids_app_js_uses(client: TestClient) -> None:
    body = await (await client.get("/")).text()
    for upstream_id in (
        "auth-disconnected",
        "auth-connected",
        "login-error",
        "auth-avatar",
        "auth-name",
        "auth-email",
        "speakers-section",
        "add-speaker-btn",
        "speakers-list",
        "add-speaker-panel",
        "speaker-error",
        "system-version",
        "system-uptime",
    ):
        assert f'id="{upstream_id}"' in body, upstream_id


async def test_index_uses_the_labels_the_docs_name(client: TestClient) -> None:
    body = await (await client.get("/")).text()
    for label in (
        "Log in to Qobuz",
        "Connect to Music Assistant",
        ">Connect</button>",
        ">Disconnect</button>",
        "All my Music Assistant players",
        "use a token instead",
        "Other speakers (DLNA / local audio)",
        "Add (MA) to the names",
    ):
        assert label in body, label


async def test_index_under_ingress_keeps_assets_relative(client: TestClient) -> None:
    resp = await client.get("/", headers={"X-Ingress-Path": "/api/hassio_ingress/abc123"})
    body = await resp.text()
    assert '<base href="/api/hassio_ingress/abc123/">' in body
    assert 'src="static/ma.js?v=9.9.9"' in body


async def test_ma_js_is_served(client: TestClient) -> None:
    resp = await client.get("/static/ma.js")
    assert resp.status == 200
    js = await resp.text()
    assert "window.qobuzDirectBase" in js
    assert '"api/ma/status"' in js


def test_ma_js_only_uses_relative_api_urls() -> None:
    for name in ("ma.js", "app.js"):
        js = (STATIC / name).read_text()
        # Absolute paths would escape the HA ingress prefix
        assert not re.search(r"""["'`]/api/""", js), name
        assert not re.search(r"""fetch\(\s*["']/""", js), name


def test_ma_js_element_ids_exist_in_index() -> None:
    html = (STATIC / "index.html").read_text()
    js = (STATIC / "ma.js").read_text()
    ids = set(re.findall(r"""\$\("([A-Za-z0-9_-]+)"\)""", js))
    assert ids, "expected $(...) lookups in ma.js"
    missing = sorted(i for i in ids if f'id="{i}"' not in html)
    assert not missing, missing


def test_app_js_login_prefers_direct_base_from_ma_js() -> None:
    js = (STATIC / "app.js").read_text()
    assert 'typeof window.qobuzDirectBase === "function"' in js


def test_style_css_has_ma_section() -> None:
    css = (STATIC / "style.css").read_text()
    for selector in (".check-list", ".toggle", ".badge-ma", ".ma-player-row"):
        assert selector in css, selector


# ---------------------------------------------------------------------------
# Route plumbing with a stub manager
# ---------------------------------------------------------------------------


async def test_status_adds_checks_and_direct_url(client: TestClient) -> None:
    resp = await client.get("/api/ma/status")
    assert resp.status == 200
    data = await resp.json()
    assert data["direct_login_url"] == "http://192.168.1.20:8689"
    assert [c["id"] for c in data["checks"]] == ["qobuz", "ma", "ma_qobuz", "exposed"]
    assert all(c["level"] in ("ok", "warn", "error", "pending") for c in data["checks"])
    assert data["players"][0]["device"]["status"] == "idle"


async def test_status_without_lan_ip_has_empty_direct_url(manager: StubManager) -> None:
    with patch("qobuz_proxy.webui.ma_routes.direct_lan_ip", return_value=""):
        async with TestClient(TestServer(make_app(manager))) as c:
            data = await (await c.get("/api/ma/status")).json()
    assert data["direct_login_url"] == ""


async def test_token_never_in_responses(client: TestClient, manager: StubManager) -> None:
    texts = [await (await client.get("/api/ma/status")).text()]
    resp = await client.post(
        "/api/ma/connect", json={"url": "http://ma:8095", "token": SECRET_TOKEN}
    )
    texts.append(await resp.text())
    resp = await client.put("/api/ma/players", json={"name_suffix": False})
    texts.append(await resp.text())
    for text in texts:
        assert SECRET_TOKEN not in text
        assert "token" not in _keys(json.loads(text))


async def test_connect_success(client: TestClient, manager: StubManager) -> None:
    resp = await client.post(
        "/api/ma/connect",
        json={"url": "http://127.0.0.1:8095", "username": "alex", "password": "pw"},
    )
    assert resp.status == 200
    data = await resp.json()
    assert data["ok"] is True
    assert data["status"]["connection"]["state"] == "connected"
    assert "checks" in data["status"]
    manager.connect.assert_awaited_once_with(
        "http://127.0.0.1:8095", username="alex", password="pw", token=""
    )


async def test_connect_error_is_400_with_message(client: TestClient, manager: StubManager) -> None:
    manager.connect_result = {"ok": False, "error": "Wrong username or password."}
    resp = await client.post("/api/ma/connect", json={"url": "", "username": "a", "password": "b"})
    assert resp.status == 400
    assert await resp.json() == {"ok": False, "error": "Wrong username or password."}


async def test_connect_rejects_non_json(client: TestClient, manager: StubManager) -> None:
    resp = await client.post("/api/ma/connect", data="not json")
    assert resp.status == 400
    assert (await resp.json())["ok"] is False
    manager.connect.assert_not_awaited()


async def test_disconnect(client: TestClient, manager: StubManager) -> None:
    resp = await client.post("/api/ma/disconnect")
    assert resp.status == 200
    assert await resp.json() == {"ok": True}
    manager.disconnect.assert_awaited_once()


async def test_players_update(client: TestClient, manager: StubManager) -> None:
    resp = await client.put(
        "/api/ma/players", json={"expose_all": False, "selected": ["up_kitchen"]}
    )
    assert resp.status == 200
    data = await resp.json()
    assert data["ok"] is True and "players" in data["status"]
    manager.update_exposure.assert_awaited_once_with(
        expose_all=False, selected=["up_kitchen"], name_suffix=None
    )


@pytest.mark.parametrize(
    "body",
    [
        {"expose_all": "yes"},
        {"name_suffix": 1},
        {"selected": "up_kitchen"},
        {"selected": [1, 2]},
        ["not", "an", "object"],
    ],
)
async def test_players_rejects_bad_types(
    client: TestClient, manager: StubManager, body: Any
) -> None:
    resp = await client.put("/api/ma/players", json=body)
    assert resp.status == 400
    assert (await resp.json())["ok"] is False
    manager.update_exposure.assert_not_awaited()


@pytest.mark.parametrize(
    "method,path",
    [
        ("GET", "/api/ma/status"),
        ("POST", "/api/ma/connect"),
        ("POST", "/api/ma/disconnect"),
        ("PUT", "/api/ma/players"),
    ],
)
async def test_routes_404_without_manager(bare_client: TestClient, method: str, path: str) -> None:
    resp = await bare_client.request(method, path, json={})
    assert resp.status == 404
    assert await resp.json() == {"error": "music_assistant_unavailable"}


async def test_index_still_served_without_manager(bare_client: TestClient) -> None:
    resp = await bare_client.get("/")
    assert resp.status == 200
    assert 'src="static/ma.js' in await resp.text()
