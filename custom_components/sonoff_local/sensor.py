"""Metric sensors.

Mapping from LAN keys to entities, for the S60TPF (UIID 190). The raw values and the
scale are documented in ``protocol.py`` and in the README.

Sensors are created the first time their key is seen rather than from a fixed model
list, because the LAN protocol does not reveal the model: ``plug`` also covers devices
with no meter, and it is not confirmed which keys every firmware sends over the LAN.
The entity registry restores them after a restart.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from homeassistant.components.sensor import (
    SensorDeviceClass,
    SensorEntity,
    SensorEntityDescription,
    SensorStateClass,
)
from homeassistant.const import (
    SIGNAL_STRENGTH_DECIBELS_MILLIWATT,
    EntityCategory,
    UnitOfElectricCurrent,
    UnitOfElectricPotential,
    UnitOfEnergy,
    UnitOfPower,
)
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback

from .coordinator import SonoffConfigEntry, SonoffCoordinator
from .entity import SonoffEntity
from .protocol import scale_metric, signal_strength

PARALLEL_UPDATES = 0

# A TOTAL_INCREASING sensor treats any decrease as a reset, so a small dip from the
# device would add a whole phantom day of energy. Upstream ignores dips this small and
# accepts larger ones as the midnight reset (SonoffLAN #1759).
ENERGY_DIP_TOLERANCE = 0.1


@dataclass(frozen=True, kw_only=True)
class SonoffSensorDescription(SensorEntityDescription):
    """Describes a sensor and the LAN keys that feed it."""

    raw_keys: tuple[str, ...]
    value_fn: Callable[[dict[str, Any]], float | int | None]


def _metric(raw_key: str) -> Callable[[dict[str, Any]], float | None]:
    return lambda state: scale_metric(state.get(raw_key))


def _energy(key: str, raw_key: str) -> SonoffSensorDescription:
    return SonoffSensorDescription(
        key=key,
        translation_key=key,
        raw_keys=(raw_key,),
        value_fn=_metric(raw_key),
        device_class=SensorDeviceClass.ENERGY,
        native_unit_of_measurement=UnitOfEnergy.KILO_WATT_HOUR,
        state_class=SensorStateClass.TOTAL_INCREASING,
        suggested_display_precision=2,
    )


SENSORS: tuple[SonoffSensorDescription, ...] = (
    SonoffSensorDescription(
        key="power",
        raw_keys=("power",),
        value_fn=_metric("power"),
        device_class=SensorDeviceClass.POWER,
        native_unit_of_measurement=UnitOfPower.WATT,
        state_class=SensorStateClass.MEASUREMENT,
        suggested_display_precision=2,
    ),
    SonoffSensorDescription(
        key="current",
        raw_keys=("current",),
        value_fn=_metric("current"),
        device_class=SensorDeviceClass.CURRENT,
        native_unit_of_measurement=UnitOfElectricCurrent.AMPERE,
        state_class=SensorStateClass.MEASUREMENT,
        suggested_display_precision=2,
    ),
    SonoffSensorDescription(
        key="voltage",
        raw_keys=("voltage",),
        value_fn=_metric("voltage"),
        device_class=SensorDeviceClass.VOLTAGE,
        native_unit_of_measurement=UnitOfElectricPotential.VOLT,
        state_class=SensorStateClass.MEASUREMENT,
        suggested_display_precision=2,
    ),
    _energy("energy_day", "dayKwh"),
    # Not in UIID 190's spec. Kept for other models, created only if actually sent.
    _energy("energy_week", "weekKwh"),
    _energy("energy_month", "monthKwh"),
    _energy("energy_year", "yearKwh"),
    SonoffSensorDescription(
        key="rssi",
        raw_keys=("rssi", "signalStrength"),
        value_fn=signal_strength,
        device_class=SensorDeviceClass.SIGNAL_STRENGTH,
        native_unit_of_measurement=SIGNAL_STRENGTH_DECIBELS_MILLIWATT,
        state_class=SensorStateClass.MEASUREMENT,
        entity_category=EntityCategory.DIAGNOSTIC,
        entity_registry_enabled_default=False,
    ),
)


async def async_setup_entry(
    hass: HomeAssistant,
    entry: SonoffConfigEntry,
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    """Add sensors now, and later whenever a new key appears."""
    coordinator = entry.runtime_data
    added: set[str] = set()

    @callback
    def add_new_sensors() -> None:
        state = coordinator.data or {}
        new = [
            description
            for description in SENSORS
            if description.key not in added
            and any(key in state for key in description.raw_keys)
        ]
        if new:
            added.update(description.key for description in new)
            async_add_entities(
                SonoffSensor(coordinator, description) for description in new
            )

    add_new_sensors()
    entry.async_on_unload(coordinator.async_add_listener(add_new_sensors))


class SonoffSensor(SonoffEntity, SensorEntity):
    """A reading from the device."""

    entity_description: SonoffSensorDescription

    def __init__(
        self, coordinator: SonoffCoordinator, description: SonoffSensorDescription
    ) -> None:
        """Create the sensor."""
        super().__init__(coordinator, description.key)
        self.entity_description = description
        self._attr_native_value = self._next_value()

    def _next_value(self) -> float | int | None:
        value = self.entity_description.value_fn(self.coordinator.data or {})
        previous = self._attr_native_value
        if value is None:
            return previous
        if (
            self.entity_description.state_class is SensorStateClass.TOTAL_INCREASING
            and previous is not None
            and -ENERGY_DIP_TOLERANCE <= value - previous < 0
        ):
            return previous
        return value

    @callback
    def _handle_coordinator_update(self) -> None:
        """Take the new reading."""
        self._attr_native_value = self._next_value()
        super()._handle_coordinator_update()
