"""
Home Assistant Supervisor helpers for finding Music Assistant.

When the proxy runs as a Home Assistant add-on, the Music Assistant add-on runs
on the same host with host networking, so MA's API is normally reachable at
``http://127.0.0.1:8095``. These helpers check that (and a few fallbacks) so the
web UI can pre-fill the address. Everything here is best-effort and never raises.
"""

import asyncio
import logging
import os
import socket
from typing import Any, Optional

import aiohttp

from .settings import DEFAULT_MA_URL

logger = logging.getLogger(__name__)

SUPERVISOR_URL = "http://supervisor"
# The MA add-on repository is built into the Supervisor (fixed hash d5369777).
MA_ADDON_SLUGS = (
    "d5369777_music_assistant",
    "d5369777_music_assistant_beta",
    "d5369777_music_assistant_nightly",
    "d5369777_music_assistant_dev",
)
CANDIDATE_URLS = (
    "http://127.0.0.1:8095",
    "http://localhost:8095",
    "http://172.30.32.1:8095",
    "http://homeassistant.local:8095",
)


def running_under_supervisor() -> bool:
    """Whether we run as a Home Assistant add-on."""
    return bool(os.environ.get("SUPERVISOR_TOKEN"))


async def _ma_addon_state(session: aiohttp.ClientSession) -> Optional[str]:
    """State of the installed MA add-on ("started", "stopped", ...), or None."""
    token = os.environ.get("SUPERVISOR_TOKEN", "")
    headers = {"Authorization": f"Bearer {token}"}
    for slug in MA_ADDON_SLUGS:
        try:
            async with session.get(
                f"{SUPERVISOR_URL}/addons/{slug}/info",
                headers=headers,
                timeout=aiohttp.ClientTimeout(total=3),
            ) as resp:
                if resp.status != 200:
                    continue
                body = await resp.json()
        except (aiohttp.ClientError, asyncio.TimeoutError, ValueError):
            continue
        data = body.get("data", {}) if isinstance(body, dict) else {}
        state = data.get("state")
        if isinstance(state, str) and state != "unknown":
            logger.debug(f"Music Assistant add-on {slug} is {state}")
            return state
    return None


async def probe_server(
    url: str, timeout: float = 2.0, session: Optional[aiohttp.ClientSession] = None
) -> Optional[dict[str, Any]]:
    """Fetch MA's unauthenticated ``/info``; returns its JSON or None."""
    own_session = session is None
    sess = session or aiohttp.ClientSession()
    try:
        async with sess.get(
            f"{url.rstrip('/')}/info", timeout=aiohttp.ClientTimeout(total=timeout)
        ) as resp:
            if resp.status != 200:
                return None
            data = await resp.json(content_type=None)
            if isinstance(data, dict) and "server_version" in data:
                return data
            return None
    except (aiohttp.ClientError, asyncio.TimeoutError, ValueError, OSError):
        return None
    finally:
        if own_session:
            await sess.close()


async def detect_music_assistant_url() -> str:
    """Best guess for the MA address; falls back to the default."""
    try:
        async with aiohttp.ClientSession() as session:
            if running_under_supervisor():
                state = await _ma_addon_state(session)
                if state is None:
                    logger.info("Music Assistant add-on not found via the Supervisor")
            for url in CANDIDATE_URLS:
                info = await probe_server(url, session=session)
                if info is not None:
                    logger.info(f"Found Music Assistant {info.get('server_version', '')} at {url}")
                    return url
    except Exception as e:  # never break the UI over detection
        logger.debug(f"Music Assistant detection failed: {e}")
    return DEFAULT_MA_URL


def direct_lan_ip() -> str:
    """The host's LAN address (the add-on uses host networking), or ""."""
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.settimeout(0)
        try:
            # Doesn't send anything, just picks the outbound interface
            s.connect(("8.8.8.8", 80))
            return str(s.getsockname()[0])
        finally:
            s.close()
    except OSError:
        return ""
