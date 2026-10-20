"""A speaker the next start would reject is refused instead of saved (it would stop the boot)."""

from unittest.mock import MagicMock, patch

import pytest

from qobuz_proxy.app import QobuzProxy
from qobuz_proxy.config import Config, SpeakerConfig


def make_app() -> QobuzProxy:
    config = Config()
    config.speakers = [SpeakerConfig(name="Living Room", dlna_ip="192.168.1.20", http_port=8690)]
    app = QobuzProxy(config)
    app._api_client = MagicMock()
    return app


async def test_unknown_backend_is_refused_and_not_saved() -> None:
    app = make_app()
    with patch.object(app, "_save_config") as save, pytest.raises(ValueError):
        await app._on_add_speaker({"name": "Kitchen", "backend": "bogus"})
    save.assert_not_called()
    assert [s.name for s in app._config.speakers] == ["Living Room"]


async def test_edit_that_drops_the_dlna_address_is_refused_and_not_saved() -> None:
    app = make_app()
    with patch.object(app, "_save_config") as save, pytest.raises(ValueError):
        await app._on_edit_speaker("living-room", {"dlna_ip": ""})
    save.assert_not_called()
    assert app._config.speakers[0].dlna_ip == "192.168.1.20"
