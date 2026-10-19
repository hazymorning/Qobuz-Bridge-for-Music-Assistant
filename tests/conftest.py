"""Shared test fixtures."""

from pathlib import Path

import pytest


@pytest.fixture(autouse=True)
def _isolated_music_assistant_data(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Keep Music Assistant settings/sessions out of the developer's data dir."""
    from qobuz_proxy.backends.music_assistant import settings

    data_dir = tmp_path / "ma-data"
    monkeypatch.setattr(settings, "default_data_dir", lambda: data_dir)
    monkeypatch.setattr(settings.SessionStore, "_shared", {})
