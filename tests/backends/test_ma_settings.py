"""Music Assistant settings and session persistence (settings.py)."""

import json
import logging
import os
import stat
from pathlib import Path
from typing import Any

import pytest

pytest.importorskip("music_assistant_models")

from qobuz_proxy.backends.music_assistant import settings  # noqa: E402
from qobuz_proxy.backends.music_assistant.settings import (  # noqa: E402
    DEFAULT_MA_URL,
    MA_PORT_RANGE_START,
    SESSIONS_FILENAME,
    SETTINGS_FILENAME,
    KnownPlayer,
    MASettings,
    MASettingsStore,
    SessionStore,
)


def full_settings() -> MASettings:
    return MASettings(
        url="http://192.168.1.5:8095",
        token="secret-long-lived-token",
        username="admin",
        expose_all=False,
        selected_player_ids=["up1", "grp"],
        name_suffix=False,
        known_players={
            "up1": KnownPlayer("up1", "Kitchen", "player", 8700),
            "grp": KnownPlayer("grp", "Downstairs", "group", 8701, hidden=True),
        },
    )


def write_json(path: Path, data: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data), encoding="utf-8")


class TestDefaults:
    def test_constants(self) -> None:
        assert DEFAULT_MA_URL == "http://127.0.0.1:8095"
        assert SETTINGS_FILENAME == "music_assistant.json"
        assert MA_PORT_RANGE_START == 8700

    def test_defaults_are_not_configured(self) -> None:
        s = MASettings()
        assert s.url == "" and s.token == "" and s.username == ""
        assert s.expose_all is True
        assert s.name_suffix is True
        assert s.selected_player_ids == []
        assert s.known_players == {}
        assert s.configured is False

    def test_configured_needs_url_and_token(self) -> None:
        assert MASettings(url="http://ma:8095").configured is False
        assert MASettings(token="t").configured is False
        assert MASettings(url="http://ma:8095", token="t").configured is True

    def test_default_path_is_in_the_data_dir(self, tmp_path: Path) -> None:
        # conftest points the data dir at tmp_path / "ma-data"
        assert MASettingsStore().path == tmp_path / "ma-data" / SETTINGS_FILENAME

    def test_default_lists_are_not_shared(self) -> None:
        a, b = MASettings(), MASettings()
        a.selected_player_ids.append("up1")
        a.known_players["up1"] = KnownPlayer("up1", "Kitchen", "player", 8700)
        assert b.selected_player_ids == [] and b.known_players == {}


class TestSettingsStore:
    def test_round_trip(self, tmp_path: Path) -> None:
        store = MASettingsStore(tmp_path / "ma.json")
        original = full_settings()

        store.save(original)
        loaded = store.load()

        assert loaded == original
        assert loaded.known_players["grp"].hidden is True
        assert loaded.known_players["up1"].hidden is False
        assert loaded.configured is True

    def test_load_from_a_fresh_instance(self, tmp_path: Path) -> None:
        path = tmp_path / "ma.json"
        MASettingsStore(path).save(full_settings())
        assert MASettingsStore(path).load() == full_settings()

    def test_missing_file_gives_defaults(self, tmp_path: Path) -> None:
        assert MASettingsStore(tmp_path / "nope.json").load() == MASettings()

    def test_corrupt_file_gives_defaults_and_warns(
        self, tmp_path: Path, caplog: pytest.LogCaptureFixture
    ) -> None:
        path = tmp_path / "ma.json"
        path.write_text("{not json", encoding="utf-8")
        with caplog.at_level(logging.WARNING, logger=settings.__name__):
            assert MASettingsStore(path).load() == MASettings()
        assert "using defaults" in caplog.text

    def test_non_object_json_gives_defaults(self, tmp_path: Path) -> None:
        path = tmp_path / "ma.json"
        write_json(path, ["url", "token"])
        assert MASettingsStore(path).load() == MASettings()

    def test_file_is_private(self, tmp_path: Path) -> None:
        path = tmp_path / "ma.json"
        MASettingsStore(path).save(full_settings())
        assert stat.S_IMODE(os.stat(path).st_mode) == 0o600

    def test_overwrite_keeps_the_file_private(self, tmp_path: Path) -> None:
        path = tmp_path / "ma.json"
        path.write_text("{}", encoding="utf-8")
        os.chmod(path, 0o644)
        MASettingsStore(path).save(full_settings())
        assert stat.S_IMODE(os.stat(path).st_mode) == 0o600

    def test_save_creates_parent_dirs(self, tmp_path: Path) -> None:
        path = tmp_path / "a" / "b" / "ma.json"
        MASettingsStore(path).save(MASettings(url="http://ma:8095"))
        assert MASettingsStore(path).load().url == "http://ma:8095"

    def test_atomic_write_leaves_no_temp_files(self, tmp_path: Path) -> None:
        path = tmp_path / "data" / "ma.json"
        store = MASettingsStore(path)
        for i in range(3):
            store.save(MASettings(url=f"http://ma{i}:8095"))
        assert sorted(p.name for p in path.parent.iterdir()) == ["ma.json"]
        assert store.load().url == "http://ma2:8095"

    def test_failed_write_keeps_the_old_file_and_cleans_up(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        path = tmp_path / "data" / "ma.json"
        store = MASettingsStore(path)
        store.save(MASettings(url="http://old:8095"))

        def boom(src: str, dst: str) -> None:
            raise OSError("disk full")

        with monkeypatch.context() as m:
            m.setattr(settings.os, "replace", boom)
            with pytest.raises(OSError):
                store.save(MASettings(url="http://new:8095"))

        assert sorted(p.name for p in path.parent.iterdir()) == ["ma.json"]
        assert store.load().url == "http://old:8095"

    def test_token_is_written_to_the_file(self, tmp_path: Path) -> None:
        # The token is needed to reconnect after a restart (the file is 0600)
        path = tmp_path / "ma.json"
        MASettingsStore(path).save(full_settings())
        assert json.loads(path.read_text())["token"] == "secret-long-lived-token"

    def test_save_never_logs_the_token(
        self, tmp_path: Path, caplog: pytest.LogCaptureFixture
    ) -> None:
        with caplog.at_level(logging.DEBUG):
            store = MASettingsStore(tmp_path / "ma.json")
            store.save(full_settings())
            store.load()
        assert "secret-long-lived-token" not in caplog.text


class TestFromDict:
    def test_known_players_hidden_flag_defaults_to_false(self) -> None:
        loaded = MASettings.from_dict(
            {
                "known_players": {
                    "up1": {"name": "Kitchen", "player_type": "player", "http_port": 8700}
                }
            }
        )
        assert loaded.known_players == {"up1": KnownPlayer("up1", "Kitchen", "player", 8700)}
        assert loaded.known_players["up1"].hidden is False

    def test_malformed_known_players_are_ignored(
        self, tmp_path: Path, caplog: pytest.LogCaptureFixture
    ) -> None:
        path = tmp_path / "ma.json"
        write_json(
            path,
            {
                "url": "http://ma:8095",
                "token": "t",
                "known_players": {
                    "good": {"name": "Kitchen", "player_type": "group", "http_port": "8705"},
                    "no_port": {"name": "Bath"},
                    "bad_port": {"name": "Hall", "http_port": "eighty"},
                    "not_a_dict": "Garage",
                    "null_port": {"name": "Attic", "http_port": None},
                },
            },
        )
        with caplog.at_level(logging.WARNING, logger=settings.__name__):
            loaded = MASettingsStore(path).load()

        assert list(loaded.known_players) == ["good"]
        assert loaded.known_players["good"] == KnownPlayer("good", "Kitchen", "group", 8705)
        assert loaded.url == "http://ma:8095" and loaded.token == "t"
        assert "no_port" in caplog.text and "bad_port" in caplog.text

    def test_known_players_not_a_dict(self) -> None:
        assert MASettings.from_dict({"known_players": ["up1"]}).known_players == {}

    def test_missing_name_falls_back_to_the_id(self) -> None:
        loaded = MASettings.from_dict({"known_players": {"up9": {"http_port": 8709}}})
        assert loaded.known_players["up9"] == KnownPlayer("up9", "up9", "player", 8709)

    def test_bad_types_are_coerced(self) -> None:
        loaded = MASettings.from_dict(
            {
                "url": None,
                "token": None,
                "username": None,
                "selected_player_ids": "up1",
                "expose_all": 0,
                "name_suffix": 1,
            }
        )
        assert loaded.url == "" and loaded.token == "" and loaded.username == ""
        assert loaded.selected_player_ids == []
        assert loaded.expose_all is False
        assert loaded.name_suffix is True

    def test_selected_ids_become_strings(self) -> None:
        assert MASettings.from_dict({"selected_player_ids": [1, "up2"]}).selected_player_ids == [
            "1",
            "up2",
        ]

    def test_to_dict_writes_every_known_player_field(self) -> None:
        data = full_settings().to_dict()
        assert data["known_players"]["grp"] == {
            "name": "Downstairs",
            "player_type": "group",
            "http_port": 8701,
            "hidden": True,
        }


class TestSessionStore:
    def test_put_get_and_remove(self, tmp_path: Path) -> None:
        store = SessionStore(tmp_path / "sessions.json")
        assert store.get("up1") is None

        session = {"current": {"item_id": "qi1", "track_id": "123"}}
        store.put("up1", session)
        assert store.get("up1") == session

        store.put("up1", None)
        assert store.get("up1") is None
        store.put("up1", None)  # removing twice is fine
        assert store.get("up1") is None

    def test_persists_across_instances(self, tmp_path: Path) -> None:
        path = tmp_path / "data" / "sessions.json"
        first = SessionStore(path)
        first.put("up1", {"current": {"item_id": "qi1"}})
        first.put("up2", {"current": {"item_id": "qi7"}})
        first.put("up2", None)

        second = SessionStore(path)
        assert second.get("up1") == {"current": {"item_id": "qi1"}}
        assert second.get("up2") is None
        assert sorted(p.name for p in path.parent.iterdir()) == ["sessions.json"]

    def test_in_memory_mode_writes_nothing(self, tmp_path: Path) -> None:
        store = SessionStore()
        store.put("up1", {"current": {"item_id": "qi1"}})
        assert store.get("up1") == {"current": {"item_id": "qi1"}}
        assert store.path is None
        assert not (tmp_path / "ma-data").exists()

    def test_unchanged_session_is_not_rewritten(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        store = SessionStore(tmp_path / "sessions.json")
        store.put("up1", {"current": {"item_id": "qi1"}})
        writes: list[str] = []
        real_mkstemp = settings.tempfile.mkstemp

        def counting_mkstemp(*args: Any, **kwargs: Any) -> Any:
            writes.append("w")
            return real_mkstemp(*args, **kwargs)

        monkeypatch.setattr(settings.tempfile, "mkstemp", counting_mkstemp)
        store.put("up1", {"current": {"item_id": "qi1"}})
        store.put("nobody", None)
        assert writes == []

    def test_corrupt_file_starts_empty(
        self, tmp_path: Path, caplog: pytest.LogCaptureFixture
    ) -> None:
        path = tmp_path / "sessions.json"
        path.write_text("[[[", encoding="utf-8")
        with caplog.at_level(logging.WARNING, logger=settings.__name__):
            store = SessionStore(path)
        assert store.get("up1") is None
        assert "without saved sessions" in caplog.text
        store.put("up1", {"current": {"item_id": "qi1"}})  # and it recovers on the next write
        assert SessionStore(path).get("up1") == {"current": {"item_id": "qi1"}}

    def test_non_dict_entries_are_dropped(self, tmp_path: Path) -> None:
        path = tmp_path / "sessions.json"
        write_json(path, {"up1": {"current": {}}, "up2": "garbage", "up3": [1, 2]})
        store = SessionStore(path)
        assert store.get("up1") == {"current": {}}
        assert store.get("up2") is None and store.get("up3") is None

    def test_default_is_shared_and_lives_in_the_data_dir(self, tmp_path: Path) -> None:
        store = SessionStore.default()
        assert store is SessionStore.default()
        assert store.path == tmp_path / "ma-data" / SESSIONS_FILENAME
        store.put("up1", {"current": {"item_id": "qi1"}})
        assert (tmp_path / "ma-data" / SESSIONS_FILENAME).exists()
