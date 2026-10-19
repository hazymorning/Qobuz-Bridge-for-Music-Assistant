"""Contract tests: the Music Assistant fake mirrors the real client's public surface.

If music-assistant-client changes a signature, these tests fail and point at the drift, so the
fake (and the production code tested against it) cannot silently diverge from the real API.
"""

from __future__ import annotations

import inspect
from typing import Any, Callable

import pytest

pytest.importorskip("music_assistant_models")
pytest.importorskip("music_assistant_client")

import music_assistant_client  # noqa: E402
import music_assistant_client.exceptions as client_exceptions  # noqa: E402
import music_assistant_models.errors as model_errors  # noqa: E402
from music_assistant_client import auth_helpers  # noqa: E402
from music_assistant_client.auth import Auth  # noqa: E402
from music_assistant_client.client import MusicAssistantClient  # noqa: E402
from music_assistant_client.connection import WebsocketsConnection  # noqa: E402
from music_assistant_client.music import Music  # noqa: E402
from music_assistant_client.player_queues import PlayerQueues  # noqa: E402
from music_assistant_client.players import Players  # noqa: E402

from tests.backends import fake_music_assistant as fake  # noqa: E402
from tests.backends.fake_music_assistant import (  # noqa: E402
    FakeMAServer,
    FakeMusicAssistantClient,
    FakeWebsocketsConnection,
)

# Members of the real client that production code (or the fake itself) relies on. The generic
# comparison below covers every public member; this list only guards against a member
# vanishing from both sides at once.
CLIENT_MEMBERS = [
    "server_info",
    "providers",
    "auth",
    "players",
    "player_queues",
    "music",
    "get_provider",
    "get_image_url",
    "get_media_item_image_url",
    "subscribe",
    "connect",
    "start_listening",
    "disconnect",
    "send_command",
    "send_command_no_wait",
    "__aenter__",
    "__aexit__",
]
PLAYERS_MEMBERS = [
    "players",
    "get",
    "__iter__",
    "__getitem__",
    "play",
    "pause",
    "play_pause",
    "stop",
    "next_track",
    "previous_track",
    "seek",
    "volume_set",
    "volume_up",
    "volume_down",
    "volume_mute",
    "power",
    "group_volume",
    "set_members",
]
QUEUES_MEMBERS = [
    "player_queues",
    "get",
    "__iter__",
    "get_queue_items",
    "get_active_queue",
    "play",
    "pause",
    "stop",
    "resume",
    "next",
    "previous",
    "play_pause",
    "seek",
    "clear",
    "delete_item",
    "move_item",
    "play_index",
    "shuffle",
    "repeat",
    "dont_stop_the_music",
    "play_media",
]
MUSIC_MEMBERS = ["get_item_by_uri", "get_track", "get_media_item_image"]
AUTH_MEMBERS = ["create_token", "get_current_user", "get_tokens", "revoke_token", "logout"]
TRANSPORT_MEMBERS = ["connected", "connect", "disconnect", "receive_message", "send_message"]


def _public_members(cls: type) -> list[str]:
    names = [name for name in dir(cls) if not name.startswith("_")]
    for dunder in ("__init__", "__aenter__", "__aexit__", "__iter__", "__getitem__"):
        if dunder in vars(cls) or any(dunder in vars(base) for base in cls.__mro__[:-1]):
            names.append(dunder)
    return sorted(set(names))


def _shape(fn: Callable[..., Any]) -> list[tuple[str, Any, Any]]:
    """Parameter names, kinds and defaults (annotations are ignored: they are strings)."""
    return [
        (param.name, param.kind, param.default)
        for param in inspect.signature(fn).parameters.values()
    ]


def _describe(cls: type, name: str) -> tuple[str, Any]:
    attr = inspect.getattr_static(cls, name)
    if isinstance(attr, property):
        return ("property", None)
    if isinstance(attr, (staticmethod, classmethod)):
        attr = attr.__func__
    if callable(attr):
        kind = "async" if inspect.iscoroutinefunction(attr) else "sync"
        return (kind, _shape(attr))
    return ("attribute", type(attr).__name__)


def _assert_same_surface(real_cls: type, fake_cls: type, required: list[str]) -> None:
    real_members = _public_members(real_cls)
    missing_required = [name for name in required if name not in real_members]
    assert not missing_required, f"{real_cls.__name__} no longer has {missing_required}"
    problems = []
    for name in real_members:
        if not hasattr(fake_cls, name):
            problems.append(f"{fake_cls.__name__}.{name} is missing")
            continue
        real_shape = _describe(real_cls, name)
        fake_shape = _describe(fake_cls, name)
        if real_shape != fake_shape:
            problems.append(
                f"{fake_cls.__name__}.{name}: real {real_shape!r} != fake {fake_shape!r}"
            )
    assert not problems, "Fake drifted from the real API:\n" + "\n".join(problems)


@pytest.fixture
def server() -> FakeMAServer:
    return FakeMAServer()


def test_fake_client_is_the_real_client(server: FakeMAServer) -> None:
    client = server.client_factory(server.url, None, server.token)
    assert isinstance(client, MusicAssistantClient)
    assert isinstance(client, FakeMusicAssistantClient)
    assert type(client.players) is Players
    assert type(client.player_queues) is PlayerQueues
    assert type(client.music) is Music
    assert type(client.auth) is Auth


def test_client_surface_matches_real(server: FakeMAServer) -> None:
    _assert_same_surface(MusicAssistantClient, server.client_class, CLIENT_MEMBERS)
    _assert_same_surface(MusicAssistantClient, FakeMusicAssistantClient, CLIENT_MEMBERS)


def test_client_constructor_signature_matches_real(server: FakeMAServer) -> None:
    assert _shape(FakeMusicAssistantClient.__init__) == _shape(MusicAssistantClient.__init__)
    assert _shape(server.client_class.__init__) == _shape(MusicAssistantClient.__init__)


def test_only_the_constructor_is_overridden() -> None:
    """Every behaviour except the transport comes from the real client class."""
    overridden = {
        name
        for name in vars(FakeMusicAssistantClient)
        if callable(vars(FakeMusicAssistantClient)[name]) and not name.startswith("__")
    }
    assert overridden == set()
    assert "__init__" in vars(FakeMusicAssistantClient)


@pytest.mark.parametrize(
    ("real_cls", "attribute", "required"),
    [
        (Players, "players", PLAYERS_MEMBERS),
        (PlayerQueues, "player_queues", QUEUES_MEMBERS),
        (Music, "music", MUSIC_MEMBERS),
        (Auth, "auth", AUTH_MEMBERS),
    ],
)
def test_controller_surface_matches_real(
    server: FakeMAServer, real_cls: type, attribute: str, required: list[str]
) -> None:
    client = server.client_factory(server.url, None, server.token)
    controller_cls = type(getattr(client, attribute))
    assert issubclass(controller_cls, real_cls)
    _assert_same_surface(real_cls, controller_cls, required)


def test_transport_surface_matches_real() -> None:
    for name in TRANSPORT_MEMBERS:
        assert (
            _describe(WebsocketsConnection, name)[0] == _describe(FakeWebsocketsConnection, name)[0]
        ), name
        if name != "connected":
            assert _describe(WebsocketsConnection, name) == _describe(
                FakeWebsocketsConnection, name
            ), name
    real = WebsocketsConnection("http://127.0.0.1:8095", object(), "t")  # type: ignore[arg-type]
    fake_conn = FakeWebsocketsConnection(FakeMAServer(), "http://127.0.0.1:8095", None, "t")
    for attribute in ("ws_server_url", "auth_token", "ssl_context"):
        assert getattr(real, attribute) == getattr(fake_conn, attribute), attribute


def test_invalid_url_fails_like_the_real_client(server: FakeMAServer) -> None:
    with pytest.raises(RuntimeError) as real_err:
        MusicAssistantClient("not-a-url", object(), "t")  # type: ignore[arg-type]
    with pytest.raises(RuntimeError) as fake_err:
        server.client_factory("not-a-url", None, "t")
    assert str(fake_err.value) == str(real_err.value)


@pytest.mark.parametrize(
    "name", ["login", "login_with_token", "create_long_lived_token", "get_server_info"]
)
def test_login_helpers_match_real(server: FakeMAServer, name: str) -> None:
    real_fn = getattr(auth_helpers, name)
    fake_fn = getattr(server, name)
    assert getattr(music_assistant_client, name) is real_fn
    assert inspect.iscoroutinefunction(fake_fn) == inspect.iscoroutinefunction(real_fn)
    assert _shape(fake_fn) == _shape(real_fn)


def test_client_factory_signature() -> None:
    """``client_factory(url, session, token)`` is what MusicAssistantConnection calls."""
    names = [name for name, _, _ in _shape(FakeMAServer().client_factory)]
    assert names == ["server_url", "aiohttp_session", "token"]


def test_reexported_exceptions_are_the_real_classes() -> None:
    real_modules = (client_exceptions, model_errors)
    checked = 0
    for name in fake.__all__:
        obj = getattr(fake, name)
        if not (isinstance(obj, type) and issubclass(obj, BaseException)):
            continue
        sources = [getattr(module, name) for module in real_modules if hasattr(module, name)]
        assert sources, f"{name} is not a real music-assistant exception"
        assert obj is sources[0], f"fake_music_assistant.{name} is not the real class"
        checked += 1
    assert checked >= 20


def test_client_and_server_errors_stay_separate_families() -> None:
    """Production must catch both: transport errors are not MusicAssistantError."""
    assert issubclass(fake.CannotConnect, fake.MusicAssistantClientException)
    assert not issubclass(fake.CannotConnect, fake.MusicAssistantError)
    assert issubclass(fake.AuthenticationFailed, fake.MusicAssistantError)
    assert not issubclass(fake.AuthenticationFailed, fake.MusicAssistantClientException)
    assert fake.CLIENT_SCHEMA_VERSION == 46
