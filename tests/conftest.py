"""Shared fixtures."""

from __future__ import annotations

from collections.abc import AsyncIterator
from typing import Any

import aiohttp
import pytest

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
