"""Web API for the panel: status checks, Music Assistant login, which players are exposed.

Responses never include a token or password.
"""

import json
import socket
from typing import Any

from aiohttp import web

from qobuz_proxy.backends.music_assistant import LoginError


def _lan_ip() -> str:
    """The host's LAN address (the add-on uses host networking), or ""."""
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
            s.connect(("8.8.8.8", 80))  # only picks the outgoing interface; sends nothing
            return str(s.getsockname()[0])
    except OSError:
        return ""


def build_checks(qobuz: dict[str, Any], status: dict[str, Any]) -> list[dict[str, str]]:
    """Status checks for the panel, in the order the user fixes them."""
    checks = []
    if qobuz["logged_in"]:
        account = qobuz["account"]
        detail = f"Logged in to Qobuz as {account}." if account else "Logged in to Qobuz."
        checks.append(("qobuz", "ok", "Qobuz account", detail))
    else:
        checks.append(
            (
                "qobuz",
                "error",
                "Qobuz account",
                "Not logged in to Qobuz yet. Tap “Log in to Qobuz” below — do it on your phone "
                "while it is on your home Wi‑Fi.",
            )
        )

    connection = status["connection"]
    state = connection["state"]
    connected = state == "connected"
    if connected:
        ma = ("ok", f"Connected to Music Assistant {connection['server_version']}.")
    elif state == "connecting":
        ma = ("pending", "Connecting to Music Assistant…")
    elif state == "auth_failed" or (state == "disconnected" and status["configured"]):
        ma = ("error", connection["message"] or "Can't reach Music Assistant.")
    else:
        ma = (
            "error",
            connection["message"]
            or "Not connected to Music Assistant yet. Enter your Music Assistant username and "
            "password below.",
        )
    checks.append(("ma", ma[0], "Music Assistant", ma[1]))

    if connected and connection["has_qobuz"]:
        checks.append(
            ("ma_qobuz", "ok", "Qobuz in Music Assistant", "Qobuz is set up in Music Assistant.")
        )
    elif connected:
        checks.append(
            (
                "ma_qobuz",
                "error",
                "Qobuz in Music Assistant",
                "Music Assistant has no working Qobuz provider. In Music Assistant open "
                "Settings → Providers and add Qobuz.",
            )
        )

    count = status["exposed_count"]
    ready = qobuz["logged_in"] and status["configured"]
    if count:
        noun = "speaker or group is" if count == 1 else "speakers and groups are"
        exposed = ("ok", f"{count} {noun} available in the Qobuz app.")
    elif ready and any(p["device_name"] for p in status["players"]):
        exposed = ("pending", "Starting the Qobuz Connect devices…")
    elif ready and connected:
        exposed = (
            "warn",
            "No speakers are shared with the Qobuz app. Turn on “All my Music Assistant "
            "players” or tick some below.",
        )
    else:
        exposed = (
            "pending",
            "Speakers appear in the Qobuz app once Qobuz and Music Assistant are both connected.",
        )
    checks.append(("exposed", exposed[0], "Speakers in the Qobuz app", exposed[1]))
    return [
        {"id": id_, "level": level, "title": title, "detail": detail}
        for id_, level, title, detail in checks
    ]


def _panel_status(request: web.Request) -> dict[str, Any]:
    status: dict[str, Any] = request.app["ma_manager"].status()
    auth = request.app["auth_state"]
    qobuz = {
        "logged_in": bool(auth["authenticated"]),
        "account": auth.get("name") or auth.get("email") or "",
    }
    ip = _lan_ip()
    return {
        **status,
        "qobuz": qobuz,
        "checks": build_checks(qobuz, status),
        "login_url": f"http://{ip}:{request.app['http_port']}" if ip else "",
    }


async def _read_body(request: web.Request, fields: dict[str, type]) -> dict[str, Any]:
    """The JSON object body, with only known fields of the right type."""
    try:
        body = await request.json()
    except ValueError:
        body = None
    if not isinstance(body, dict):
        raise _bad_request("Invalid request.")
    for key, value in body.items():
        if key not in fields or not isinstance(value, fields[key]):
            raise _bad_request(f"Invalid field: {key}"[:100])
    return body


def _bad_request(error: str) -> web.HTTPBadRequest:
    return web.HTTPBadRequest(text=json.dumps({"error": error}), content_type="application/json")


async def _handle_status(request: web.Request) -> web.Response:
    return web.json_response(_panel_status(request))


async def _handle_connect(request: web.Request) -> web.Response:
    body = await _read_body(request, {"url": str, "username": str, "password": str})
    try:
        await request.app["ma_manager"].connect(
            body.get("url", ""), body.get("username", ""), body.get("password", "")
        )
    except LoginError as e:
        raise _bad_request(str(e)) from None
    return web.json_response(_panel_status(request))


async def _handle_disconnect(request: web.Request) -> web.Response:
    await request.app["ma_manager"].disconnect()
    return web.json_response(_panel_status(request))


async def _handle_players(request: web.Request) -> web.Response:
    body = await _read_body(request, {"expose_all": bool, "selected": list})
    selected = body.get("selected")
    if selected is not None and not all(isinstance(p, str) for p in selected):
        raise _bad_request("Invalid field: selected")
    await request.app["ma_manager"].set_exposure(body.get("expose_all"), selected)
    return web.json_response(_panel_status(request))


def register_ma_routes(app: web.Application) -> None:
    app.router.add_get("/api/ma/status", _handle_status)
    app.router.add_post("/api/ma/connect", _handle_connect)
    app.router.add_post("/api/ma/disconnect", _handle_disconnect)
    app.router.add_put("/api/ma/players", _handle_players)
