"""Base entity."""

from __future__ import annotations

from homeassistant.helpers.device_registry import DeviceInfo
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from .const import CONF_LOCAL_TYPE, DOMAIN
from .coordinator import SonoffCoordinator


class SonoffEntity(CoordinatorEntity[SonoffCoordinator]):
    """An entity belonging to one device."""

    _attr_has_entity_name = True

    def __init__(self, coordinator: SonoffCoordinator, key: str) -> None:
        """Attach the entity to its device."""
        super().__init__(coordinator)
        entry = coordinator.config_entry
        firmware = (coordinator.data or {}).get("fwVersion")
        self._attr_unique_id = f"{coordinator.deviceid}_{key}"
        self._attr_device_info = DeviceInfo(
            identifiers={(DOMAIN, coordinator.deviceid)},
            manufacturer="SONOFF",
            # The LAN protocol only exposes a coarse type, not the product name.
            model=entry.data.get(CONF_LOCAL_TYPE),
            name=entry.title,
            serial_number=coordinator.deviceid,
            sw_version=firmware if isinstance(firmware, str) else None,
        )
