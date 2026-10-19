"""Discovery must advertise and receive queries on the same LAN interface."""

import json
from unittest.mock import MagicMock, patch

import pytest

from qobuz_proxy.config import Config
from qobuz_proxy.connect.discovery import DiscoveryService


@pytest.mark.parametrize("name", ["Kitchen", "Living Room"])
async def test_mdns_joins_only_the_advertised_interface(name):
    config = Config()
    config.device.name = name
    discovery = DiscoveryService(config, "test")
    with (
        patch.object(discovery, "_get_local_ip", return_value="10.0.1.9"),
        patch("qobuz_proxy.connect.discovery.Zeroconf") as zeroconf,
    ):
        await discovery._register_mdns()
        # No InterfaceChoice.All/default: hosts with many Docker bridges must
        # not consume the socket's membership budget before reaching the LAN.
        zeroconf.assert_called_once_with(interfaces=["10.0.1.9"])
        info = zeroconf.return_value.register_service.call_args.args[0]
        assert info.parsed_addresses() == ["10.0.1.9"]
        assert info.port == config.server.http_port
        assert info.properties[b"Name"].decode() == name
        await discovery._unregister_mdns()
        zeroconf.return_value.unregister_service.assert_called_once_with(info)
        zeroconf.return_value.close.assert_called_once()


async def test_no_resolved_address_does_not_fall_back_to_all_interfaces():
    discovery = DiscoveryService(Config(), "test")
    with (
        patch.object(discovery, "_get_local_ip", return_value=None),
        patch("qobuz_proxy.connect.discovery.Zeroconf") as zeroconf,
    ):
        await discovery._register_mdns()
        zeroconf.assert_not_called()


async def test_conflicting_service_reuses_the_scoped_socket():
    discovery = DiscoveryService(Config(), "test")
    zc = MagicMock()
    zc.register_service.side_effect = [RuntimeError("name conflict"), None]
    with (
        patch.object(discovery, "_get_local_ip", return_value="10.0.1.9"),
        patch("qobuz_proxy.connect.discovery.Zeroconf", return_value=zc) as zeroconf,
    ):
        await discovery._register_mdns()
        zeroconf.assert_called_once_with(interfaces=["10.0.1.9"])
        assert zc.register_service.call_args.kwargs == {"cooperating_responders": True}


@pytest.mark.parametrize(
    ("device_type", "expected"),
    [("speaker", "SPEAKER"), ("tv", "TV"), ("streamer", "STREAMER"), ("soundbar", "SOUNDBAR")],
)
async def test_device_type_is_advertised(device_type, expected):
    config = Config()
    config.device.device_type = device_type
    discovery = DiscoveryService(config, "test")

    response = await discovery._handle_display_info(MagicMock())
    assert json.loads(response.text)["type"] == expected

    with (
        patch.object(discovery, "_get_local_ip", return_value="10.0.1.9"),
        patch("qobuz_proxy.connect.discovery.Zeroconf") as zeroconf,
    ):
        await discovery._register_mdns()
        info = zeroconf.return_value.register_service.call_args.args[0]
        assert info.properties[b"type"].decode() == expected
