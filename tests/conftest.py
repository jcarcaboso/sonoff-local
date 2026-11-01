"""Shared fixtures."""

from __future__ import annotations

from collections.abc import AsyncIterator, Iterator
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import aiohttp
import pytest
from homeassistant.const import CONF_HOST, CONF_PORT
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.sonoff_local.const import (
    CONF_DEVICE_ID,
    CONF_DEVICE_KEY,
    CONF_LOCAL_TYPE,
    DOMAIN,
)

from .fake_device import FakeS60TPF


@pytest.fixture(autouse=True)
def auto_enable_custom_integrations(enable_custom_integrations: Any) -> None:
    """Allow Home Assistant to load the integration under test."""


@pytest.fixture
async def device(socket_enabled: Any) -> AsyncIterator[FakeS60TPF]:
    """Run a fake encrypted S60TPF."""
    fake = FakeS60TPF()
    await fake.start()
    yield fake
    await fake.stop()


@pytest.fixture
async def diy_device(socket_enabled: Any) -> AsyncIterator[FakeS60TPF]:
    """Run a fake device in unencrypted mode with a DIY-style state."""
    fake = FakeS60TPF(
        devicekey=None,
        state={
            "switch": "on",
            "startup": "stay",
            "pulse": "off",
            "pulseWidth": 500,
            "ssid": "home-wifi",
            "otaUnlock": False,
            "fwVersion": "3.5.0",
            "deviceid": "100226eeda",
            "bssid": "aa:bb:cc:dd:ee:ff",
            "signalStrength": -67,
        },
    )
    await fake.start()
    yield fake
    await fake.stop()


@pytest.fixture
async def session(socket_enabled: Any) -> AsyncIterator[aiohttp.ClientSession]:
    """Provide a plain client session."""
    async with aiohttp.ClientSession() as client_session:
        yield client_session


@pytest.fixture
def mock_mdns() -> Iterator[MagicMock]:
    """Replace the browser, and report nothing in the mDNS cache."""
    info = MagicMock()
    info.return_value.properties = {}
    info.return_value.async_request = AsyncMock(return_value=False)
    with (
        patch("custom_components.sonoff_local.mdns.AsyncServiceBrowser") as browser,
        patch("custom_components.sonoff_local.mdns.AsyncServiceInfo", info),
        patch(
            "homeassistant.components.zeroconf.async_get_async_instance",
            AsyncMock(return_value=MagicMock()),
        ),
    ):
        browser.return_value.async_cancel = AsyncMock()
        browser.info = info
        yield browser


@pytest.fixture(autouse=True)
def stub_zeroconf_dependency(request: pytest.FixtureRequest) -> Iterator[None]:
    """Stop the zeroconf dependency from opening network sockets during setup."""
    with patch("homeassistant.components.zeroconf.async_setup", return_value=True):
        yield


def entry_for(fake: FakeS60TPF, **options: Any) -> MockConfigEntry:
    """Return a config entry pointing at a fake device."""
    return MockConfigEntry(
        domain=DOMAIN,
        unique_id=fake.deviceid,
        title=f"Sonoff {fake.deviceid}",
        data={
            CONF_HOST: fake.host,
            CONF_PORT: fake.port,
            CONF_DEVICE_ID: fake.deviceid,
            CONF_DEVICE_KEY: fake.devicekey,
            CONF_LOCAL_TYPE: "plug",
        },
        options=options,
    )
