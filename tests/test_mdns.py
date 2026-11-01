"""Tests for the mDNS hub, using the real zeroconf library.

Records are fed through a real ``Zeroconf`` instance rather than a mock, because the
behaviour that matters (a TXT record with no address record still being readable) is
the library's, and a mock would only repeat my assumption about it.
"""

from __future__ import annotations

import asyncio
import socket
from collections.abc import AsyncIterator
from typing import Any
from unittest.mock import AsyncMock, patch

import pytest
from homeassistant.core import HomeAssistant
from zeroconf import (
    DNSAddress,
    DNSOutgoing,
    DNSPointer,
    DNSService,
    DNSText,
    ServiceStateChange,
    Zeroconf,
    const,
)
from zeroconf._protocol.incoming import DNSIncoming

from custom_components.sonoff_local.mdns import (
    MdnsEvent,
    SonoffMdnsHub,
    async_lookup_txt,
    async_read_service,
)
from custom_components.sonoff_local.protocol import SERVICE_TYPE, decode_txt

from .fake_device import DEVICE_ID, DEVICE_KEY, SERVICE_NAME, FakeS60TPF

TTL = 120
SERVER = f"eWeLink_{DEVICE_ID}.local."


def encode_txt(properties: dict[str, str]) -> bytes:
    """Encode TXT properties in DNS wire format."""
    encoded = b""
    for key, value in properties.items():
        item = f"{key}={value}".encode()
        encoded += bytes([len(item)]) + item
    return encoded


def announcement(
    properties: dict[str, str], *, address: str | None, name: str = SERVICE_NAME
) -> DNSIncoming:
    """Build a response packet. ``address=None`` models the faulty unit in #1871."""
    out = DNSOutgoing(const._FLAGS_QR_RESPONSE | const._FLAGS_AA)
    out.add_answer_at_time(
        DNSPointer(SERVICE_TYPE, const._TYPE_PTR, const._CLASS_IN, TTL, name), 0
    )
    out.add_answer_at_time(
        DNSService(
            name,
            const._TYPE_SRV,
            const._CLASS_IN | const._CLASS_UNIQUE,
            TTL,
            0,
            0,
            8081,
            SERVER,
        ),
        0,
    )
    out.add_answer_at_time(
        DNSText(
            name,
            const._TYPE_TXT,
            const._CLASS_IN | const._CLASS_UNIQUE,
            TTL,
            encode_txt(properties),
        ),
        0,
    )
    if address is not None:
        out.add_answer_at_time(
            DNSAddress(
                SERVER,
                const._TYPE_A,
                const._CLASS_IN | const._CLASS_UNIQUE,
                TTL,
                socket.inet_aton(address),
            ),
            0,
        )
    return DNSIncoming(out.packets()[0])


@pytest.fixture
async def real_zeroconf(socket_enabled: Any) -> AsyncIterator[Zeroconf]:
    """Run a real zeroconf instance bound to loopback."""
    zc = Zeroconf(interfaces=["127.0.0.1"])
    await zc.async_wait_for_start()
    yield zc
    await zc._async_close()


@pytest.fixture
def hub_zeroconf(real_zeroconf: Zeroconf) -> Any:
    """Make Home Assistant's accessor return the real instance."""
    wrapper = AsyncMock()
    wrapper.zeroconf = real_zeroconf
    with patch(
        "homeassistant.components.zeroconf.async_get_async_instance",
        AsyncMock(return_value=wrapper),
    ):
        yield real_zeroconf


async def deliver(zc: Zeroconf, packet: DNSIncoming) -> None:
    """Hand a packet to zeroconf as if it arrived from the network."""
    zc.record_manager.async_updates_from_response(packet)
    await asyncio.sleep(0.05)


async def test_reads_txt_with_no_address_record(real_zeroconf: Zeroconf) -> None:
    """SonoffLAN #1871: PTR, SRV and TXT with no A record is still usable."""
    device = FakeS60TPF()
    await deliver(real_zeroconf, announcement(device.txt(), address=None))

    event = await async_read_service(real_zeroconf, SERVICE_NAME)
    assert event is not None
    assert event.host is None
    assert event.port == 8081
    assert event.frame is not None
    assert decode_txt(event.frame, DEVICE_KEY)["voltage"] == 23717


async def test_reads_address_when_present(real_zeroconf: Zeroconf) -> None:
    """A normal unit also gives its address."""
    device = FakeS60TPF()
    await deliver(real_zeroconf, announcement(device.txt(), address="192.0.2.10"))
    event = await async_read_service(real_zeroconf, SERVICE_NAME)
    assert event is not None
    assert event.host == "192.0.2.10"


async def test_lookup_uses_cache_only(
    hass: HomeAssistant, hub_zeroconf: Zeroconf
) -> None:
    """The config flow can read a cached record, and misses return None."""
    device = FakeS60TPF()
    await deliver(hub_zeroconf, announcement(device.txt(), address=None))
    frame = await async_lookup_txt(hass, DEVICE_ID)
    assert frame is not None
    assert frame.encrypted is True
    assert await async_lookup_txt(hass, "ffffffffff") is None


async def test_hub_delivers_addressless_updates(
    hass: HomeAssistant, hub_zeroconf: Zeroconf
) -> None:
    """End to end through the real browser, for the faulty unit."""
    device = FakeS60TPF()
    events: list[MdnsEvent] = []
    hub = SonoffMdnsHub(hass)
    unsubscribe = await hub.async_subscribe(DEVICE_ID, events.append)
    await asyncio.sleep(0.05)

    await deliver(hub_zeroconf, announcement(device.txt({"power": 1}), address=None))
    await asyncio.sleep(0.25)  # The browser batches add events.
    await hass.async_block_till_done(wait_background_tasks=True)
    assert len(events) == 1
    assert events[0].frame is not None
    assert events[0].host is None

    await deliver(hub_zeroconf, announcement(device.txt({"power": 2}), address=None))
    await asyncio.sleep(0.25)
    await hass.async_block_till_done(wait_background_tasks=True)
    assert len(events) == 2
    assert events[1].frame is not None
    assert decode_txt(events[1].frame, DEVICE_KEY) == {"power": 2}

    unsubscribe()
    await hass.async_block_till_done(wait_background_tasks=True)


async def test_hub_ignores_unsubscribed_and_foreign_services(
    hass: HomeAssistant, hub_zeroconf: Zeroconf
) -> None:
    """Only subscribed devices are delivered."""
    events: list[MdnsEvent] = []
    hub = SonoffMdnsHub(hass)
    unsubscribe = await hub.async_subscribe(DEVICE_ID, events.append)
    await asyncio.sleep(0.05)

    other = FakeS60TPF(deviceid="ffffffffff")
    await deliver(
        hub_zeroconf,
        announcement(
            other.txt(), address=None, name=f"eWeLink_ffffffffff.{SERVICE_TYPE}"
        ),
    )
    await deliver(
        hub_zeroconf,
        announcement(
            other.txt(), address=None, name=f"ihost-1001000000.{SERVICE_TYPE}"
        ),
    )
    await asyncio.sleep(0.25)
    await hass.async_block_till_done(wait_background_tasks=True)
    assert events == []
    unsubscribe()
    await hass.async_block_till_done(wait_background_tasks=True)


async def test_hub_shares_one_browser(
    hass: HomeAssistant, hub_zeroconf: Zeroconf
) -> None:
    """Several devices share a browser, which stops with the last one."""
    hub = SonoffMdnsHub(hass)
    first = await hub.async_subscribe(DEVICE_ID, lambda _event: None)
    browser = hub._browser
    second = await hub.async_subscribe("ffffffffff", lambda _event: None)
    assert hub._browser is browser

    first()
    await hass.async_block_till_done(wait_background_tasks=True)
    assert hub._browser is browser

    second()
    await hass.async_block_till_done(wait_background_tasks=True)
    assert hub._browser is None


async def test_hub_reports_removal(hass: HomeAssistant, hub_zeroconf: Zeroconf) -> None:
    """A goodbye reaches the device."""
    events: list[MdnsEvent] = []
    hub = SonoffMdnsHub(hass)
    unsubscribe = await hub.async_subscribe(DEVICE_ID, events.append)
    await asyncio.sleep(0.05)

    hub._async_on_change(
        zeroconf=hub_zeroconf,
        service_type=SERVICE_TYPE,
        name=SERVICE_NAME,
        state_change=ServiceStateChange.Removed,
    )
    assert events == [MdnsEvent(removed=True)]
    unsubscribe()
    await hass.async_block_till_done(wait_background_tasks=True)
