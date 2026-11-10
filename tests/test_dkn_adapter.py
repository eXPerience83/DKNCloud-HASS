"""Synthetic wire contracts; no service connections or account data."""

import asyncio
import json
from unittest.mock import Mock

import pytest
from yarl import URL

from custom_components.airzoneclouddaikin.push_adapter import (
    build_verified_adapter,
    decode_device,
)
from custom_components.airzoneclouddaikin.pusher_client import PusherProtocolError


@pytest.mark.parametrize(
    "payload", [{"id": "fake", "power": "1"}, json.dumps({"id": "fake", "power": "1"})]
)
def test_device_data_encoding(payload):
    assert decode_device(payload) == {"id": "fake", "power": "1"}


@pytest.mark.parametrize(
    "payload", ["private-invalid-json", [], {}, {"device": {"id": "fake"}}]
)
def test_unknown_envelope_is_rejected_without_retaining_decoder_context(payload):
    with pytest.raises(PusherProtocolError) as error:
        decode_device(payload)
    assert error.value.__context__ is None
    assert "private-invalid-json" not in str(error.value)


def test_factory_is_operational_and_uses_current_api(monkeypatch):
    monkeypatch.setattr(
        "custom_components.airzoneclouddaikin.push_adapter.PUSHER_APP_KEY",
        "synthetic-app",
    )
    api, session = Mock(), Mock()
    adapter = build_verified_adapter(api, session)
    assert adapter is not None
    assert URL(adapter._url).origin() == URL("wss://dkn.airzonecloud.com:8080")
    assert "/app/synthetic-app?" in adapter._url
    assert "protocol=7" in adapter._url
    assert adapter._authorize is api.authorize_channel


async def test_custom_decoder_failure_does_not_retain_event_data(monkeypatch):
    from types import SimpleNamespace
    from unittest.mock import AsyncMock

    from custom_components.airzoneclouddaikin.push_adapter import DknPushAdapter

    class Wire:
        def __init__(
            self, session, url, channels, authorize, on_device, on_subscription
        ):
            self.on_device = on_device

        async def connect(self):
            await self.on_device("fake", "synthetic-invalid-sensitive-payload")

    monkeypatch.setattr(
        "custom_components.airzoneclouddaikin.push_adapter.PusherClient", Wire
    )
    owner = SimpleNamespace(
        state=SimpleNamespace(generation=0),
        _limit=asyncio.Semaphore(2),
        _stopped=False,
        _auth_failed=False,
        schedulers={"fake": None},
        event=AsyncMock(),
        subscription=AsyncMock(),
    )
    adapter = DknPushAdapter(
        Mock(), "wss://example.invalid/app/synthetic", AsyncMock(), json.loads
    )
    with pytest.raises(PusherProtocolError) as error:
        await adapter.connect(owner)
    assert error.value.__context__ is None
    owner.event.assert_not_awaited()
