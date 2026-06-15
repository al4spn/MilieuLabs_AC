"""Sensor platform for Milieu Labs AC integration."""
import logging
import uuid
from homeassistant.core import callback
from homeassistant.components.sensor import (
    SensorDeviceClass,
    SensorEntity,
    SensorStateClass,
)
from homeassistant.const import (
    PERCENTAGE,
    UnitOfPressure,
)
from homeassistant.helpers.typing import StateType
from homeassistant.helpers.update_coordinator import CoordinatorEntity
from homeassistant.helpers.entity import DeviceInfo
from .const import DOMAIN

_LOGGER = logging.getLogger(__name__)


async def async_setup_entry(hass, config_entry, async_add_entities) -> None:
    """Set up Milieu Labs AC hub sensors from a config entry."""
    _LOGGER.debug("Setting up sensors for entry: %s", config_entry.entry_id)
    coordinator = hass.data[DOMAIN][config_entry.entry_id]["coordinator"]

    sensors = [
        MilieuACHubHumidity(coordinator),
        MilieuACHubPressure(coordinator),
        MilieuACHubCO2(coordinator),
    ]

    async_add_entities(sensors, True)

    # Store the sensors in hass.data for reference
    hass.data[DOMAIN][config_entry.entry_id]["sensors"].extend(sensors)
    _LOGGER.info("Added %s hub sensors for Milieu Labs AC", len(sensors))


# ---------------------------------------------------------------------------
# Hub shadow sensors – live values pushed via MQTT from the hub device shadow
# ---------------------------------------------------------------------------

class MilieuACHubSensorBase(CoordinatorEntity, SensorEntity):
    """Base for sensors sourced from the hub device shadow (MQTT)."""

    _attr_has_entity_name = True

    def __init__(self, coordinator, name: str, hub_key: str) -> None:
        super().__init__(coordinator, context=f"hub_{hub_key}")
        self._hub_key = hub_key
        self._attr_unique_id = str(
            uuid.uuid5(uuid.NAMESPACE_DNS, f"{DOMAIN}_hub_{hub_key}")
        )
        self._attr_name = name
        self._attr_device_info = DeviceInfo(
            identifiers={(DOMAIN, coordinator.hub_shadow_name)},
            name="Milieu Labs Hub",
            manufacturer="Milieu Labs",
            model="Hub",
        )

    @callback
    def _handle_coordinator_update(self) -> None:
        self.async_write_ha_state()

    @property
    def native_value(self) -> StateType:
        return self.coordinator.hub_shadow_data.get(self._hub_key)

    @property
    def available(self) -> bool:
        return self._hub_key in self.coordinator.hub_shadow_data

    @property
    def extra_state_attributes(self) -> dict:
        return {
            "source": "hub_shadow",
        }


class MilieuACHubHumidity(MilieuACHubSensorBase):
    """Humidity sensor sourced from hub shadow BME280."""

    _attr_device_class = SensorDeviceClass.HUMIDITY
    _attr_state_class = SensorStateClass.MEASUREMENT
    _attr_native_unit_of_measurement = PERCENTAGE
    _attr_suggested_display_precision = 1

    def __init__(self, coordinator) -> None:
        super().__init__(coordinator, "Humidity", "humidity")


class MilieuACHubPressure(MilieuACHubSensorBase):
    """Pressure sensor sourced from hub shadow BME280."""

    _attr_device_class = SensorDeviceClass.PRESSURE
    _attr_state_class = SensorStateClass.MEASUREMENT
    _attr_native_unit_of_measurement = UnitOfPressure.HPA
    _attr_suggested_display_precision = 1

    def __init__(self, coordinator) -> None:
        super().__init__(coordinator, "Pressure", "pressure")


class MilieuACHubCO2(MilieuACHubSensorBase):
    """CO2 sensor sourced from hub shadow iAQ."""

    _attr_device_class = SensorDeviceClass.CO2
    _attr_state_class = SensorStateClass.MEASUREMENT
    _attr_native_unit_of_measurement = "ppm"
    _attr_suggested_display_precision = 0

    def __init__(self, coordinator) -> None:
        super().__init__(coordinator, "CO2", "co2")
