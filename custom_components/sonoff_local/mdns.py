"""Shared mDNS browser that delivers TXT updates to configured devices.

Home Assistant's own discovery cannot be used for this. It ignores services with no
address record, and some S60TPF units never send one while still announcing valid
TXT data (SonoffLAN #1871). So TXT records are read from the shared zeroconf cache,
which holds them regardless, and no extra sockets are opened.
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from dataclasses import dataclass

from homeassistant.components import zeroconf
from homeassistant.core import HomeAssistant, callback
from homeassistant.util.hass_dict import HassKey
from zeroconf import IPVersion, ServiceStateChange, Zeroconf
from zeroconf.asyncio import AsyncServiceBrowser, AsyncServiceInfo

from .const import DOMAIN
from .protocol import SERVICE_TYPE, TxtFrame, parse_device_id, parse_txt

_LOGGER = logging.getLogger(__name__)

_DATA_HUB: HassKey[SonoffMdnsHub] = HassKey(f"{DOMAIN}_mdns")
# How long to ask the network when the cache has nothing, in milliseconds.
_REQUEST_TIMEOUT_MS = 3000


@dataclass(frozen=True, slots=True)
class MdnsEvent:
    """A change to a device's mDNS service."""

    frame: TxtFrame | None = None
    host: str | None = None
    port: int | None = None
    removed: bool = False


MdnsCallback = Callable[[MdnsEvent], None]


def _service_name(deviceid: str) -> str:
    return f"eWeLink_{deviceid}.{SERVICE_TYPE}"


def _event_from_info(info: AsyncServiceInfo) -> MdnsEvent | None:
    """Build an event. A missing address is fine, the TXT is still useful."""
    if not info.properties:
        return None
    addresses = info.parsed_addresses(IPVersion.V4Only)
    return MdnsEvent(
        frame=parse_txt(info.properties),
        host=addresses[0] if addresses else None,
        port=info.port or None,
    )


async def async_read_service(zc: Zeroconf, name: str) -> MdnsEvent | None:
    """Read a service, preferring the cache over asking the network."""
    info = AsyncServiceInfo(SERVICE_TYPE, name)
    info.load_from_cache(zc)
    if not info.properties:
        await info.async_request(zc, _REQUEST_TIMEOUT_MS)
    return _event_from_info(info)


async def async_lookup_txt(hass: HomeAssistant, deviceid: str) -> TxtFrame | None:
    """Return the cached TXT frame for a device, without using the network."""
    aiozc = await zeroconf.async_get_async_instance(hass)
    info = AsyncServiceInfo(SERVICE_TYPE, _service_name(deviceid))
    info.load_from_cache(aiozc.zeroconf)
    return event.frame if (event := _event_from_info(info)) else None


class SonoffMdnsHub:
    """Runs one browser for all configured devices."""

    def __init__(self, hass: HomeAssistant) -> None:
        """Create the hub."""
        self._hass = hass
        self._subscribers: dict[str, MdnsCallback] = {}
        self._browser: AsyncServiceBrowser | None = None

    async def async_subscribe(
        self, deviceid: str, target: MdnsCallback
    ) -> Callable[[], None]:
        """Deliver events for a device until the returned function is called."""
        self._subscribers[deviceid] = target
        if self._browser is None:
            aiozc = await zeroconf.async_get_async_instance(self._hass)
            self._browser = AsyncServiceBrowser(
                aiozc.zeroconf, SERVICE_TYPE, handlers=[self._async_on_change]
            )

        @callback
        def unsubscribe() -> None:
            self._subscribers.pop(deviceid, None)
            if not self._subscribers:
                self._hass.async_create_background_task(
                    self._async_stop_browser(), f"{DOMAIN} stop mDNS browser"
                )

        return unsubscribe

    async def _async_stop_browser(self) -> None:
        if self._subscribers or self._browser is None:
            return
        browser, self._browser = self._browser, None
        await browser.async_cancel()

    @callback
    def _async_on_change(
        self,
        zeroconf: Zeroconf,
        service_type: str,
        name: str,
        state_change: ServiceStateChange,
    ) -> None:
        """Handle a browser event."""
        deviceid = parse_device_id(name)
        if deviceid is None or (target := self._subscribers.get(deviceid)) is None:
            return
        if state_change is ServiceStateChange.Removed:
            target(MdnsEvent(removed=True))
            return
        self._hass.async_create_background_task(
            self._async_process(zeroconf, name, deviceid),
            f"{DOMAIN} mDNS {deviceid}",
        )

    async def _async_process(self, zc: Zeroconf, name: str, deviceid: str) -> None:
        """Read a service and pass it on."""
        try:
            event = await async_read_service(zc, name)
        except Exception:
            _LOGGER.debug("Could not read mDNS service %s", name, exc_info=True)
            return
        if event is None:
            return
        if (target := self._subscribers.get(deviceid)) is not None:
            target(event)


async def async_get_hub(hass: HomeAssistant) -> SonoffMdnsHub:
    """Return the shared hub."""
    if (hub := hass.data.get(_DATA_HUB)) is None:
        hub = hass.data[_DATA_HUB] = SonoffMdnsHub(hass)
    return hub
