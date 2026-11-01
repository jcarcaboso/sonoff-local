"""Relay switch."""

from __future__ import annotations

from typing import Any

from homeassistant.components.switch import SwitchDeviceClass, SwitchEntity
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback

from .coordinator import SonoffConfigEntry, SonoffCoordinator
from .entity import SonoffEntity
from .protocol import switch_is_on

PARALLEL_UPDATES = 1


async def async_setup_entry(
    hass: HomeAssistant,
    entry: SonoffConfigEntry,
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    """Add the relay. Every supported device has one, so state is not needed yet."""
    async_add_entities([SonoffSwitch(entry.runtime_data)])


class SonoffSwitch(SonoffEntity, SwitchEntity):
    """The relay on outlet 0."""

    _attr_name = None
    _attr_device_class = SwitchDeviceClass.OUTLET

    def __init__(self, coordinator: SonoffCoordinator) -> None:
        """Create the switch."""
        super().__init__(coordinator, "switch")

    @property
    def is_on(self) -> bool | None:
        """Return the relay state, or None if the device has not reported it."""
        return switch_is_on(self.coordinator.data or {})

    async def async_turn_on(self, **kwargs: Any) -> None:
        """Turn on."""
        await self.coordinator.async_set_switch(True)

    async def async_turn_off(self, **kwargs: Any) -> None:
        """Turn off."""
        await self.coordinator.async_set_switch(False)
