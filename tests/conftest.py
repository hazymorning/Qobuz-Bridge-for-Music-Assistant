"""Shared test fixtures."""

from pathlib import Path

import pytest


@pytest.fixture(autouse=True)
def _isolated_music_assistant_settings(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Keep the app's Music Assistant settings out of the developer's data dir."""
    monkeypatch.setattr("qobuz_proxy.app.CACHE_DIR", tmp_path)
