"""
Music Assistant settings, kept as one private JSON file in the data directory.

Holds the MA address and long-lived token, which MA players are exposed, the
discovery port of every player exposed so far, the names the user gave their
speakers, and for each player the MA queue items the bridge created (so an
add-on restart can continue them).
"""

import json
import logging
import os
import tempfile
from dataclasses import asdict, dataclass, field, fields
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

DEFAULT_URL = "http://127.0.0.1:8095"


@dataclass
class KnownPlayer:
    """An MA player that was exposed before; keeps its speaker and port across restarts."""

    name: str
    player_type: str  # "player", "stereo_pair" or "group"
    http_port: int  # Qobuz Connect discovery port
    hidden: bool = False


@dataclass
class Settings:
    url: str = ""
    token: str = ""  # secret: never logged, never sent to the web UI
    username: str = ""
    expose_all: bool = True
    selected_player_ids: list[str] = field(default_factory=list)
    known_players: dict[str, KnownPlayer] = field(default_factory=dict)
    speaker_names: dict[str, str] = field(default_factory=dict)  # player id -> the user's name
    sessions: dict[str, dict[str, Any]] = field(default_factory=dict)

    @property
    def is_configured(self) -> bool:
        return bool(self.url and self.token)


_FIELDS = {f.name for f in fields(Settings)}


def load(path: Path) -> Settings:
    """Read the settings; a missing or unreadable file gives the defaults."""
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        data["known_players"] = {
            player_id: KnownPlayer(**known)
            for player_id, known in data.get("known_players", {}).items()
        }
        return Settings(**{k: v for k, v in data.items() if k in _FIELDS})
    except FileNotFoundError:
        return Settings()
    except (OSError, ValueError, TypeError, AttributeError) as e:
        logger.warning(f"Could not read {path} ({type(e).__name__}); starting from scratch")
        return Settings()


def save(path: Path, settings: Settings) -> None:
    """Write the settings atomically, readable by the owner only (mkstemp makes it 0600)."""
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=f".{path.name}.", suffix=".tmp")
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as f:
                json.dump(asdict(settings), f, indent=2)
            os.replace(tmp, path)
        except BaseException:
            os.unlink(tmp)
            raise
    except OSError as e:
        logger.error(f"Could not save the Music Assistant settings to {path}: {e}")
