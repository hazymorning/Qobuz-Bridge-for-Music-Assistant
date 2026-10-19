"""Tests for DLNAClient Sonos queue actions."""

from unittest.mock import AsyncMock, MagicMock

import pytest

from qobuz_proxy.backends.dlna.client import DLNAClient, DLNAClientError

ADD_URI_RESPONSE = """<?xml version="1.0"?>
<s:Envelope xmlns:s="http://schemas.xmlsoap.org/soap/envelope/">
  <s:Body>
    <u:AddURIToQueueResponse xmlns:u="urn:schemas-upnp-org:service:AVTransport:1">
      <FirstTrackNumberEnqueued>5</FirstTrackNumberEnqueued>
      <NumTracksAdded>1</NumTracksAdded>
      <NewQueueLength>5</NewQueueLength>
    </u:AddURIToQueueResponse>
  </s:Body>
</s:Envelope>"""


def _make_client() -> DLNAClient:
    client = DLNAClient("10.0.0.5")
    client.device_info = MagicMock(av_transport_url="http://10.0.0.5:1400/AVTransport/Control")
    return client


class TestConnect:
    async def test_closes_session_when_description_fetch_fails(self):
        client = DLNAClient("10.0.0.5")
        session = AsyncMock()
        client._build_session = MagicMock(return_value=session)  # type: ignore[method-assign]
        client._fetch_device_description = AsyncMock(  # type: ignore[method-assign]
            side_effect=DLNAClientError("unreachable")
        )

        with pytest.raises(DLNAClientError):
            await client.connect()

        session.close.assert_awaited_once()
        assert client._session is None

    async def test_closes_session_when_device_lacks_av_transport(self):
        client = DLNAClient("10.0.0.5")
        session = AsyncMock()
        client._build_session = MagicMock(return_value=session)  # type: ignore[method-assign]
        client._fetch_device_description = AsyncMock(  # type: ignore[method-assign]
            return_value=MagicMock(av_transport_url="")
        )

        with pytest.raises(DLNAClientError):
            await client.connect()

        session.close.assert_awaited_once()
        assert client._session is None


class TestTimeStringParsing:
    def test_parses_hms(self):
        client = DLNAClient("10.0.0.5")
        assert client._time_string_to_ms("0:03:25") == 205000
        assert client._time_string_to_ms("01:00:00.500") == 3600500

    def test_not_implemented_returns_none(self):
        """Regression for BUG-10: NOT_IMPLEMENTED must read as unknown, not 0,
        or it stomps the last known position on every poll."""
        client = DLNAClient("10.0.0.5")
        assert client._time_string_to_ms("NOT_IMPLEMENTED") is None
        assert client._time_string_to_ms("") is None
        assert client._time_string_to_ms("garbage") is None

    async def test_position_info_unparseable_reltime_is_none(self):
        client = _make_client()
        client._soap_action = AsyncMock(  # type: ignore[method-assign]
            return_value="<RelTime>NOT_IMPLEMENTED</RelTime>"
        )

        assert await client.get_position_info() is None


class TestAddUriToQueue:
    async def test_returns_enqueued_track_number(self):
        client = _make_client()
        client._soap_action = AsyncMock(return_value=ADD_URI_RESPONSE)  # type: ignore[method-assign]

        result = await client.add_uri_to_queue("http://proxy/audio/222_9.flac")

        assert result == 5

    async def test_returns_none_on_failure(self):
        client = _make_client()
        client._soap_action = AsyncMock(return_value=None)  # type: ignore[method-assign]

        assert await client.add_uri_to_queue("http://proxy/audio/222_9.flac") is None


class TestRemoveTrackFromQueue:
    async def test_sends_object_id_for_track_number(self):
        client = _make_client()
        client._soap_action = AsyncMock(return_value="<ok/>")  # type: ignore[method-assign]

        assert await client.remove_track_from_queue(5)

        args = client._soap_action.await_args.args
        assert args[2] == "RemoveTrackFromQueue"
        assert args[3]["ObjectID"] == "Q:0/5"

    async def test_returns_false_on_failure(self):
        client = _make_client()
        client._soap_action = AsyncMock(return_value=None)  # type: ignore[method-assign]

        assert await client.remove_track_from_queue(5) is False


MEDIA_INFO_RESPONSE = """<?xml version="1.0"?>
<s:Envelope xmlns:s="http://schemas.xmlsoap.org/soap/envelope/">
  <s:Body>
    <u:GetMediaInfoResponse xmlns:u="urn:schemas-upnp-org:service:AVTransport:1">
      <NrTracks>1</NrTracks>
      <CurrentURI>{uri}</CurrentURI>
      <NextURI></NextURI>
    </u:GetMediaInfoResponse>
  </s:Body>
</s:Envelope>"""


class TestGetMediaInfo:
    async def test_returns_current_uri(self):
        client = _make_client()
        client._soap_action = AsyncMock(  # type: ignore[method-assign]
            return_value=MEDIA_INFO_RESPONSE.format(uri="http://proxy/audio/1.flac")
        )
        assert await client.get_media_info() == "http://proxy/audio/1.flac"

    async def test_empty_uri_is_distinct_from_a_failed_request(self):
        """upmpdcli 1.9 reports an empty CurrentURI once stopped."""
        client = _make_client()
        client._soap_action = AsyncMock(  # type: ignore[method-assign]
            return_value=MEDIA_INFO_RESPONSE.format(uri="")
        )
        assert await client.get_media_info() == ""

        client._soap_action = AsyncMock(return_value=None)  # type: ignore[method-assign]
        assert await client.get_media_info() is None
