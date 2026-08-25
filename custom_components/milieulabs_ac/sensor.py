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
    LIGHT_LUX,
    PERCENTAGE,
    SIGNAL_STRENGTH_DECIBELS_MILLIWATT,
    EntityCategory,
    UnitOfElectricPotential,
    UnitOfPressure,
    UnitOfTemperature,
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
        MilieuACHubTemperature(coordinator),
        MilieuACHubHumidity(coordinator),
        MilieuACHubPressure(coordinator),
        MilieuACHubCO2(coordinator),
        MilieuACHubVOC(coordinator),
        MilieuACHubAQI(coordinator),
        MilieuACHubIlluminance(coordinator),
        MilieuACHubWifiRSSI(coordinator),
        MilieuACHubBatteryVoltage(coordinator),
        MilieuACHubBoardHotTemp(coordinator),
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
            uuid.uuid5(
                uuid.NAMESPACE_DNS,
                f"{DOMAIN}_{coordinator.hub_shadow_name}_hub_{hub_key}",
            )
        )
        self._attr_name = name
        self._attr_device_info = DeviceInfo(
            identifiers={(DOMAIN, coordinator.hub_shadow_name)},
            name=f"{coordinator.hub_name} Hub",
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
        return (
            self._hub_key in self.coordinator.hub_shadow_data
            and self.coordinator.hub_fresh
        )

    @property
    def extra_state_attributes(self) -> dict:
        return {
            "source": "hub_shadow",
        }


class MilieuACHubTemperature(MilieuACHubSensorBase):
    """Temperature sensor sourced from hub shadow BME280.

    This is the wall-mounted sensor the thermostat itself reads, and it is
    the value the climate entity reports as its current temperature.
    """

    _attr_device_class = SensorDeviceClass.TEMPERATURE
    _attr_state_class = SensorStateClass.MEASUREMENT
    _attr_native_unit_of_measurement = UnitOfTemperature.CELSIUS
    _attr_suggested_display_precision = 1

    def __init__(self, coordinator) -> None:
        super().__init__(coordinator, "Temperature", "temperature")


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


class MilieuACHubVOC(MilieuACHubSensorBase):
    """VOC index sourced from hub shadow iAQ.

    A unitless index, not a ppb concentration, so no device_class is set.
    """

    _attr_state_class = SensorStateClass.MEASUREMENT
    _attr_icon = "mdi:molecule"

    def __init__(self, coordinator) -> None:
        super().__init__(coordinator, "VOC", "voc")


class MilieuACHubAQI(MilieuACHubSensorBase):
    """Air quality index sourced from hub shadow iAQ."""

    _attr_device_class = SensorDeviceClass.AQI
    _attr_state_class = SensorStateClass.MEASUREMENT
    _attr_suggested_display_precision = 1

    def __init__(self, coordinator) -> None:
        super().__init__(coordinator, "Air Quality Index", "air_quality_index")


class MilieuACHubIlluminance(MilieuACHubSensorBase):
    """Ambient light sourced from hub shadow ISL29023."""

    _attr_device_class = SensorDeviceClass.ILLUMINANCE
    _attr_state_class = SensorStateClass.MEASUREMENT
    _attr_native_unit_of_measurement = LIGHT_LUX
    _attr_suggested_display_precision = 0

    def __init__(self, coordinator) -> None:
        super().__init__(coordinator, "Illuminance", "illuminance")


class MilieuACHubWifiRSSI(MilieuACHubSensorBase):
    """Hub Wi-Fi signal strength — diagnostic."""

    _attr_device_class = SensorDeviceClass.SIGNAL_STRENGTH
    _attr_state_class = SensorStateClass.MEASUREMENT
    _attr_native_unit_of_measurement = SIGNAL_STRENGTH_DECIBELS_MILLIWATT
    _attr_entity_category = EntityCategory.DIAGNOSTIC
    _attr_entity_registry_enabled_default = False

    def __init__(self, coordinator) -> None:
        super().__init__(coordinator, "Wi-Fi Signal", "wifi_rssi")


class MilieuACHubBatteryVoltage(MilieuACHubSensorBase):
    """Hub battery voltage sourced from hub shadow GASGAUGE — diagnostic."""

    _attr_device_class = SensorDeviceClass.VOLTAGE
    _attr_state_class = SensorStateClass.MEASUREMENT
    _attr_native_unit_of_measurement = UnitOfElectricPotential.VOLT
    _attr_suggested_display_precision = 2
    _attr_entity_category = EntityCategory.DIAGNOSTIC
    _attr_entity_registry_enabled_default = False

    def __init__(self, coordinator) -> None:
        super().__init__(coordinator, "Battery Voltage", "battery_voltage")


class MilieuACHubBoardHotTemp(MilieuACHubSensorBase):
    """Board hot-side NTC temperature — diagnostic.

    Runs well above ambient; useful for understanding the thermal gradient the
    reported BME280 temperature is corrected against, not as a room reading.
    """

    _attr_device_class = SensorDeviceClass.TEMPERATURE
    _attr_state_class = SensorStateClass.MEASUREMENT
    _attr_native_unit_of_measurement = UnitOfTemperature.CELSIUS
    _attr_suggested_display_precision = 1
    _attr_entity_category = EntityCategory.DIAGNOSTIC
    _attr_entity_registry_enabled_default = False

    def __init__(self, coordinator) -> None:
        super().__init__(coordinator, "Board Hot-Side Temperature", "board_hot_temp")
