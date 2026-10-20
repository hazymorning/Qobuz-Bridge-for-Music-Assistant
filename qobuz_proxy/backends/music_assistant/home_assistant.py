"""
Home Assistant Core, reached through the Supervisor, for party speakers grouped there.

The add-on gets a Supervisor token in SUPERVISOR_TOKEN; with "homeassistant_api"
in config.json that token also opens Home Assistant's REST API. Outside Home
Assistant there is no token and this stays unavailable.
"""

import asyncio
import logging
import os
from typing import Any, NamedTuple, Optional

import aiohttp

logger = logging.getLogger(__name__)

DEFAULT_URL = "http://supervisor/core/api"
TIMEOUT_SECONDS = 10.0
GROUPING_FEATURE = 524288  # MediaPlayerEntityFeature.GROUPING
UNEXPECTED_ANSWER = "Home Assistant sent an answer the add-on does not understand."
NOT_AVAILABLE = "Home Assistant can only group speakers when the add-on runs in Home Assistant."


class HomeAssistantError(Exception):
    """A Home Assistant problem; the message is plain language for the panel."""


class MediaPlayer(NamedTuple):
    entity_id: str
    name: str
    state: str  # "playing", "idle", "off", "unavailable", ...
    can_group: bool
    group_members: list[str]  # the entity ids grouped with it, itself included


class HomeAssistant:
    """A small client for Home Assistant's media players."""

    def __init__(self, token: Optional[str] = None, url: str = DEFAULT_URL) -> None:
        self._token = os.environ.get("SUPERVISOR_TOKEN", "") if token is None else token
        self._url = url

    @property
    def available(self) -> bool:
        return bool(self._token)

    async def media_players(self) -> list[MediaPlayer]:
        states = await self._request("GET", "/states")
        if not isinstance(states, list):
            raise HomeAssistantError(UNEXPECTED_ANSWER)
        players = []
        for entity in states:
            if not isinstance(entity, dict):
                continue
            entity_id = str(entity.get("entity_id", ""))
            if not entity_id.startswith("media_player."):
                continue
            attributes = entity.get("attributes")
            if not isinstance(attributes, dict):
                attributes = {}
            features = attributes.get("supported_features")
            members = attributes.get("group_members")
            players.append(
                MediaPlayer(
                    entity_id=entity_id,
                    name=str(attributes.get("friendly_name") or entity_id),
                    state=str(entity.get("state", "")),
                    can_group=isinstance(features, int) and bool(features & GROUPING_FEATURE),
                    group_members=[str(m) for m in members] if isinstance(members, list) else [],
                )
            )
        return players

    async def join(self, main_entity_id: str, member_ids: list[str]) -> None:
        """Group the members with the main player."""
        await self._request(
            "POST",
            "/services/media_player/join",
            {"entity_id": main_entity_id, "group_members": member_ids},
        )

    async def unjoin(self, entity_id: str) -> None:
        """Take one player out of its group."""
        await self._request("POST", "/services/media_player/unjoin", {"entity_id": entity_id})

    async def _request(self, method: str, path: str, body: Optional[dict[str, Any]] = None) -> Any:
        if not self.available:
            raise HomeAssistantError(NOT_AVAILABLE)
        logger.info(f"To Home Assistant: {method} {path} {body or ''}".rstrip())
        headers = {"Authorization": f"Bearer {self._token}"}
        timeout = aiohttp.ClientTimeout(total=TIMEOUT_SECONDS)
        try:
            async with aiohttp.ClientSession(timeout=timeout) as http:
                async with http.request(
                    method, self._url + path, json=body, headers=headers
                ) as response:
                    if response.status in (401, 403):
                        raise HomeAssistantError(
                            "Home Assistant did not let the add-on in. Update the add-on so it "
                            "gets access to Home Assistant."
                        )
                    if response.status >= 400:
                        raise HomeAssistantError(
                            f"Home Assistant answered {response.status} {response.reason}."
                        )
                    return await response.json(content_type=None)
        except (aiohttp.ClientError, asyncio.TimeoutError, OSError) as e:
            raise HomeAssistantError("Can't reach Home Assistant.") from e
        except ValueError as e:
            raise HomeAssistantError(UNEXPECTED_ANSWER) from e
