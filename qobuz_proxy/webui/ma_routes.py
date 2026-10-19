"""Web UI API routes for the Music Assistant integration.

The panel uses these to connect to Music Assistant, choose which MA players
appear in the Qobuz app, and show plain-language status checks. Responses never
include the MA token.
"""

import logging
import time
from typing import Any, Optional

from aiohttp import web

from qobuz_proxy.backends.music_assistant.supervisor import direct_lan_ip

logger = logging.getLogger(__name__)


def _manager(request: web.Request) -> Optional[Any]:
    return request.app.get("ma_manager")


def _unavailable() -> web.Response:
    return web.json_response({"error": "music_assistant_unavailable"}, status=404)


def build_checks(auth_state: dict, status: dict) -> list[dict[str, str]]:
    """Status checks for the panel, in the order the user fixes them."""
    checks: list[dict[str, str]] = []

    if auth_state.get("authenticated"):
        who = auth_state.get("name") or auth_state.get("email") or ""
        checks.append(
            {
                "id": "qobuz",
                "level": "ok",
                "title": "Qobuz account",
                "detail": f"Logged in to Qobuz as {who}." if who else "Logged in to Qobuz.",
            }
        )
    else:
        checks.append(
            {
                "id": "qobuz",
                "level": "error",
                "title": "Qobuz account",
                "detail": "Not logged in to Qobuz yet. Tap “Log in to Qobuz” below — do it on "
                "your phone while it is on your home Wi‑Fi.",
            }
        )

    connection = status.get("connection", {})
    state = connection.get("state", "not_configured")
    message = connection.get("message", "")
    connected = state == "connected"
    if not status.get("supported", True):
        ma = {"level": "error", "detail": "Music Assistant support is not installed in this build."}
    elif connected:
        version = connection.get("server_version", "")
        ma = {
            "level": "ok",
            "detail": f"Connected to Music Assistant {version}.".replace("  ", " "),
        }
    elif state == "connecting":
        ma = {"level": "pending", "detail": "Connecting to Music Assistant…"}
    elif state == "auth_failed":
        ma = {"level": "error", "detail": message}
    elif state == "disconnected" and status.get("configured"):
        ma = {"level": "error", "detail": message or "Can't reach Music Assistant."}
    else:
        ma = {
            "level": "error",
            "detail": "Not connected to Music Assistant yet. Enter your Music Assistant "
            "username and password below.",
        }
    checks.append({"id": "ma", "title": "Music Assistant", **ma})

    if connected:
        if connection.get("qobuz_provider"):
            checks.append(
                {
                    "id": "ma_qobuz",
                    "level": "ok",
                    "title": "Qobuz in Music Assistant",
                    "detail": "Qobuz is set up in Music Assistant.",
                }
            )
        else:
            checks.append(
                {
                    "id": "ma_qobuz",
                    "level": "error",
                    "title": "Qobuz in Music Assistant",
                    "detail": "Music Assistant has no working Qobuz provider. In Music Assistant "
                    "open Settings → Providers and add Qobuz.",
                }
            )

    count = int(status.get("exposed_count", 0))
    if count:
        noun = "speaker or group is" if count == 1 else "speakers and groups are"
        exposed = {
            "level": "ok",
            "detail": f"{count} {noun} available in the Qobuz app.",
        }
    elif (
        auth_state.get("authenticated")
        and status.get("configured")
        and (connected or any(p.get("exposed") for p in status.get("players", [])))
    ):
        if any(p.get("exposed") for p in status.get("players", [])):
            exposed = {
                "level": "pending",
                "detail": "Starting the Qobuz Connect devices…",
            }
        else:
            exposed = {
                "level": "warn",
                "detail": "No speakers are shared with the Qobuz app. Turn on “All my Music "
                "Assistant players” or tick some below.",
            }
    else:
        exposed = {
            "level": "pending",
            "detail": "Speakers appear in the Qobuz app once Qobuz and Music Assistant are "
            "both connected.",
        }
    checks.append({"id": "exposed", "title": "Speakers in the Qobuz app", **exposed})
    return checks


class _LanIpCache:
    """The host's LAN address, looked up at most once a minute (the panel polls)."""

    def __init__(self) -> None:
        self._checked_at = 0.0
        self._ip = ""

    def get(self) -> str:
        if not self._ip or time.monotonic() - self._checked_at > 60.0:
            self._ip = direct_lan_ip()
            self._checked_at = time.monotonic()
        return self._ip


def _status_payload(request: web.Request, manager: Any) -> dict[str, Any]:
    status: dict[str, Any] = manager.status()
    status["checks"] = build_checks(request.app.get("auth_state", {}), status)
    cache = request.app.get("ma_lan_ip")
    ip = cache.get() if isinstance(cache, _LanIpCache) else direct_lan_ip()
    port = request.app.get("http_port", 8689)
    status["direct_login_url"] = f"http://{ip}:{port}" if ip else ""
    return status


async def _handle_status(request: web.Request) -> web.Response:
    manager = _manager(request)
    if manager is None:
        return _unavailable()
    return web.json_response(_status_payload(request, manager))


async def _read_json(request: web.Request) -> Optional[dict]:
    try:
        body = await request.json()
    except Exception:
        return None
    return body if isinstance(body, dict) else None


async def _handle_connect(request: web.Request) -> web.Response:
    manager = _manager(request)
    if manager is None:
        return _unavailable()
    body = await _read_json(request)
    if body is None:
        return web.json_response({"ok": False, "error": "Invalid request."}, status=400)
    url = str(body.get("url", "") or "")
    username = str(body.get("username", "") or "")
    password = str(body.get("password", "") or "")
    token = str(body.get("token", "") or "")
    result = await manager.connect(url, username=username, password=password, token=token)
    if not result.get("ok"):
        return web.json_response({"ok": False, "error": result.get("error", "")}, status=400)
    return web.json_response({"ok": True, "status": _status_payload(request, manager)})


async def _handle_disconnect(request: web.Request) -> web.Response:
    manager = _manager(request)
    if manager is None:
        return _unavailable()
    await manager.disconnect()
    return web.json_response({"ok": True})


async def _handle_players(request: web.Request) -> web.Response:
    manager = _manager(request)
    if manager is None:
        return _unavailable()
    body = await _read_json(request)
    if body is None:
        return web.json_response({"ok": False, "error": "Invalid request."}, status=400)
    expose_all = body.get("expose_all")
    selected = body.get("selected")
    name_suffix = body.get("name_suffix")
    if expose_all is not None and not isinstance(expose_all, bool):
        return web.json_response(
            {"ok": False, "error": "expose_all must be true/false"}, status=400
        )
    if name_suffix is not None and not isinstance(name_suffix, bool):
        return web.json_response(
            {"ok": False, "error": "name_suffix must be true/false"}, status=400
        )
    if selected is not None and (
        not isinstance(selected, list) or not all(isinstance(p, str) for p in selected)
    ):
        return web.json_response(
            {"ok": False, "error": "selected must be a list of ids"}, status=400
        )
    await manager.update_exposure(expose_all=expose_all, selected=selected, name_suffix=name_suffix)
    return web.json_response({"ok": True, "status": _status_payload(request, manager)})


def register_ma_routes(app: web.Application) -> None:
    """Register the Music Assistant API routes."""
    app["ma_lan_ip"] = _LanIpCache()
    app.router.add_get("/api/ma/status", _handle_status)
    app.router.add_post("/api/ma/connect", _handle_connect)
    app.router.add_post("/api/ma/disconnect", _handle_disconnect)
    app.router.add_put("/api/ma/players", _handle_players)
