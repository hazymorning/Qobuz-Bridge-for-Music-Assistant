"""The Music Assistant settings file (settings.load / settings.save)."""

import json
import os
import stat
from pathlib import Path

import pytest

from qobuz_proxy.backends.music_assistant import settings
from qobuz_proxy.backends.music_assistant.settings import KnownPlayer, Settings, load, save


def full_settings() -> Settings:
    return Settings(
        url="http://192.168.1.5:8095",
        token="secret-long-lived-token",
        username="admin",
        expose_all=False,
        selected_player_ids=["up1", "grp"],
        known_players={
            "up1": KnownPlayer("Kitchen", "player", 8700),
            "grp": KnownPlayer("Downstairs", "group", 8701, hidden=True),
        },
        sessions={"up1": {"current": {"item_id": "qi1", "track_id": "101", "duration_ms": 1}}},
    )


def test_round_trip(tmp_path: Path) -> None:
    path = tmp_path / "data" / "ma.json"  # the folder does not exist yet
    save(path, full_settings())

    loaded = load(path)

    assert loaded == full_settings()
    assert loaded.is_configured


def test_missing_file_gives_defaults(tmp_path: Path) -> None:
    loaded = load(tmp_path / "nope.json")
    assert loaded == Settings()
    assert not loaded.is_configured


@pytest.mark.parametrize(
    "content",
    ["{not json", '["url", "token"]', '{"url": "u", "known_players": {"up1": {"name": "K"}}}'],
    ids=["not_json", "not_an_object", "broken_known_player"],
)
def test_corrupt_file_gives_defaults(tmp_path: Path, content: str) -> None:
    path = tmp_path / "ma.json"
    path.write_text(content, encoding="utf-8")
    assert load(path) == Settings()


def test_file_is_readable_by_the_owner_only(tmp_path: Path) -> None:
    path = tmp_path / "ma.json"
    path.write_text("{}", encoding="utf-8")
    os.chmod(path, 0o644)

    save(path, full_settings())

    assert stat.S_IMODE(os.stat(path).st_mode) == 0o600


def test_unknown_keys_from_an_older_file_are_ignored(tmp_path: Path) -> None:
    path = tmp_path / "ma.json"
    older = {
        "url": "http://ma:8095",
        "token": "t",
        "name_suffix": False,
        "known_players": {
            "up1": {"name": "Kitchen", "player_type": "player", "http_port": 8700, "hidden": False}
        },
    }
    path.write_text(json.dumps(older), encoding="utf-8")

    loaded = load(path)

    assert (loaded.url, loaded.token) == ("http://ma:8095", "t")
    assert loaded.known_players == {"up1": KnownPlayer("Kitchen", "player", 8700)}


def test_failed_write_keeps_the_old_file(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    path = tmp_path / "ma.json"
    save(path, Settings(url="http://old:8095"))

    def disk_full(src: str, dst: str) -> None:
        raise OSError("disk full")

    with monkeypatch.context() as m:
        m.setattr(settings.os, "replace", disk_full)
        save(path, Settings(url="http://new:8095"))  # logged, not raised

    assert load(path).url == "http://old:8095"
    assert [p.name for p in tmp_path.iterdir()] == ["ma.json"]  # no temp file left
