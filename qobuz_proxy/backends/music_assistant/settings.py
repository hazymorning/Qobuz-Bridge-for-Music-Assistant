"""
Music Assistant settings persisted in the data directory.

The MA address, the long-lived MA token and which MA players are exposed as
Qobuz Connect devices are chosen in the web UI and stored as JSON next to
credentials.json (``/data`` in the Home Assistant add-on). The token is a
secret: it is never logged and never returned by the web API.
"""

import json
import logging
import os
import tempfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Optional

logger = logging.getLogger(__name__)

DEFAULT_MA_URL = "http://127.0.0.1:8095"
SETTINGS_FILENAME = "music_assistant.json"
# Discovery ports for MA devices start here, clear of the YAML speakers (8690+).
MA_PORT_RANGE_START = 8700


@dataclass
class KnownPlayer:
    """An MA player we have exposed before; keeps its port stable across restarts."""

    player_id: str
    name: str  # MA name last seen
    player_type: str  # "player" | "group" | "stereo_pair"
    http_port: int  # Qobuz Connect discovery port
    hidden: bool = False  # hidden in MA's UI


@dataclass
class MASettings:
    """Music Assistant connection and exposure settings."""

    url: str = ""  # empty until the user connected once
    token: str = ""  # long-lived MA token (secret)
    username: str = ""  # display only
    expose_all: bool = True
    selected_player_ids: list[str] = field(default_factory=list)
    name_suffix: bool = True  # append " (MA)" to the Qobuz device names
    known_players: dict[str, KnownPlayer] = field(default_factory=dict)

    @property
    def configured(self) -> bool:
        """Whether there is an address and a token to connect with."""
        return bool(self.url and self.token)

    def to_dict(self) -> dict[str, Any]:
        """Serialize for the settings file (includes the token)."""
        return {
            "url": self.url,
            "token": self.token,
            "username": self.username,
            "expose_all": self.expose_all,
            "selected_player_ids": list(self.selected_player_ids),
            "name_suffix": self.name_suffix,
            "known_players": {
                pid: {
                    "name": kp.name,
                    "player_type": kp.player_type,
                    "http_port": kp.http_port,
                    "hidden": kp.hidden,
                }
                for pid, kp in self.known_players.items()
            },
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "MASettings":
        """Build settings from the file contents, ignoring malformed entries."""
        known: dict[str, KnownPlayer] = {}
        raw_known = data.get("known_players")
        if isinstance(raw_known, dict):
            for pid, raw in raw_known.items():
                if not isinstance(raw, dict):
                    continue
                try:
                    known[str(pid)] = KnownPlayer(
                        player_id=str(pid),
                        name=str(raw.get("name", pid)),
                        player_type=str(raw.get("player_type", "player")),
                        http_port=int(raw["http_port"]),
                        hidden=bool(raw.get("hidden", False)),
                    )
                except (KeyError, TypeError, ValueError):
                    logger.warning(f"Ignoring malformed Music Assistant player entry: {pid}")
        selected = data.get("selected_player_ids")
        return cls(
            url=str(data.get("url", "") or ""),
            token=str(data.get("token", "") or ""),
            username=str(data.get("username", "") or ""),
            expose_all=bool(data.get("expose_all", True)),
            selected_player_ids=[str(p) for p in selected] if isinstance(selected, list) else [],
            name_suffix=bool(data.get("name_suffix", True)),
            known_players=known,
        )


def default_data_dir() -> Path:
    """Data directory, same rule as credentials.json (evaluated at call time)."""
    return Path(os.environ.get("QOBUZPROXY_DATA_DIR", Path.home() / ".qobuz-proxy"))


class MASettingsStore:
    """Load and save :class:`MASettings` as a private JSON file."""

    def __init__(self, path: Optional[Path] = None) -> None:
        self.path = path or default_data_dir() / SETTINGS_FILENAME

    def load(self) -> MASettings:
        """Read the settings; a missing or corrupt file gives the defaults."""
        if not self.path.exists():
            return MASettings()
        try:
            with open(self.path, encoding="utf-8") as f:
                data = json.load(f)
        except (OSError, ValueError) as e:
            logger.warning(f"Could not read {self.path} ({e}); using defaults")
            return MASettings()
        if not isinstance(data, dict):
            logger.warning(f"Unexpected content in {self.path}; using defaults")
            return MASettings()
        return MASettings.from_dict(data)

    def save(self, settings: MASettings) -> None:
        """Write the settings atomically, readable by the owner only."""
        self.path.parent.mkdir(parents=True, exist_ok=True)
        fd, tmp = tempfile.mkstemp(dir=self.path.parent, prefix=".ma-settings-", suffix=".tmp")
        try:
            os.fchmod(fd, 0o600)
            with os.fdopen(fd, "w", encoding="utf-8") as f:
                json.dump(settings.to_dict(), f, indent=2)
            os.replace(tmp, self.path)
        except OSError as e:
            logger.error(f"Could not save Music Assistant settings to {self.path}: {e}")
            try:
                os.unlink(tmp)
            except OSError:
                pass
            raise
