"""Sonoff Local: control Sonoff devices over the LAN, with no cloud account."""

from __future__ import annotations

from homeassistant.const import Platform
from homeassistant.core import HomeAssistant

from .coordinator import SonoffConfigEntry, SonoffCoordinator
from .mdns import async_get_hub

PLATFORMS: list[Platform] = [Platform.SENSOR, Platform.SWITCH]


async def async_setup_entry(hass: HomeAssistant, entry: SonoffConfigEntry) -> bool:
    """Set up a device."""
    coordinator = SonoffCoordinator(hass, entry)

    # Subscribe first, so a push that arrives during setup is not lost.
    hub = await async_get_hub(hass)
    entry.async_on_unload(
        await hub.async_subscribe(coordinator.deviceid, coordinator.async_handle_mdns)
    )

    await coordinator.async_config_entry_first_refresh()

    entry.runtime_data = coordinator
    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)
    return True


async def async_unload_entry(hass: HomeAssistant, entry: SonoffConfigEntry) -> bool:
    """Unload a device."""
    return await hass.config_entries.async_unload_platforms(entry, PLATFORMS)
