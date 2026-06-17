"""Number platform for Milieu Labs AC zone setpoints."""
import logging
import uuid

from homeassistant.components.number import NumberEntity, NumberMode
from homeassistant.const import UnitOfTemperature
from homeassistant.core import callback
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from .const import DOMAIN

_LOGGER = logging.getLogger(__name__)

# Configuration for each setpoint type
SETPOINT_CONFIGS: dict[str, dict] = {
    "userSetCoolSetPoint_dC": {
        "label": "Cool Setpoint",
        "min": 16.0,
        "max": 32.0,
        "step": 0.5,
        "icon": "mdi:thermometer-chevron-up",
    },
    "userSetHeatSetPoint_dC": {
        "label": "Heat Setpoint",
        "min": 16.0,
        "max": 30.0,
        "step": 0.5,
        "icon": "mdi:thermometer-chevron-down",
    },
}


async def async_setup_entry(hass, config_entry, async_add_entities) -> None:
    """Set up zone setpoint number entities from a config entry."""
    _LOGGER.debug("Setting up zone setpoint numbers for entry: %s", config_entry.entry_id)
    coordinator = hass.data[DOMAIN][config_entry.entry_id]["coordinator"]

    # Register this callback so _async_notify_zones can add future zones
    coordinator._async_add_zone_number_entities = async_add_entities

    # Immediately create entities for zones that are already known
    entities = [
        MilieuACZoneSetpoint(coordinator, zone_id, key)
        for zone_id in list(coordinator.zone_data.keys())
        for key in SETPOINT_CONFIGS
    ]

    if entities:
        async_add_entities(entities, True)
        _LOGGER.info(
            "Added %d zone setpoint entity(ies) for already-known zones",
            len(entities),
        )
    else:
        _LOGGER.debug(
            "No zones known yet – setpoint entities will be created when zones arrive via MQTT"
        )


class MilieuACZoneSetpoint(CoordinatorEntity, NumberEntity):
    """Number entity representing a zone cool or heat setpoint.

    The value is stored in the shadow as deci-°C (integer) under
    ``reported.Zone.zones.<zone_id>.<key>`` and is presented to
    Home Assistant as °C with 0.5 °C resolution.
    """

    _attr_native_unit_of_measurement = UnitOfTemperature.CELSIUS
    _attr_mode = NumberMode.BOX

    def __init__(self, coordinator, zone_id: str, key: str) -> None:
        """Initialise the setpoint entity."""
        super().__init__(coordinator, context=f"{zone_id}_{key}")
        self._zone_id = zone_id
        self._key = key

        cfg = SETPOINT_CONFIGS[key]
        self._attr_native_min_value = cfg["min"]
        self._attr_native_max_value = cfg["max"]
        self._attr_native_step = cfg["step"]
        self._attr_icon = cfg["icon"]
        self._attr_unique_id = str(
            uuid.uuid5(uuid.NAMESPACE_DNS, f"{DOMAIN}_{zone_id}_{key}")
        )

    # ------------------------------------------------------------------
    # Properties
    # ------------------------------------------------------------------

    @property
    def _zone_display_name(self) -> str:
        """Return the human-readable zone name."""
        return (
            self.coordinator.zone_data.get(self._zone_id, {}).get("name")
            or self._zone_id
        )

    @property
    def name(self) -> str:
        """Entity name: '<Zone name> Cool/Heat Setpoint'."""
        return f"{self._zone_display_name} {SETPOINT_CONFIGS[self._key]['label']}"

    @property
    def available(self) -> bool:
        """Available as soon as the zone exists in coordinator data."""
        return self._zone_id in self.coordinator.zone_data

    @property
    def native_value(self) -> float | None:
        """Current setpoint in °C (converted from deci-°C)."""
        raw = self.coordinator.zone_data.get(self._zone_id, {}).get("raw", {})
        val = raw.get(self._key)
        if val is None:
            return None
        return round(val / 10.0, 1)

    @property
    def extra_state_attributes(self) -> dict:
        """Expose raw deci-°C value for diagnostics."""
        raw = self.coordinator.zone_data.get(self._zone_id, {}).get("raw", {})
        return {
            "zone_id": self._zone_id,
            "raw_value_dC": raw.get(self._key),
            "lvr_shadow_name": self.coordinator.lvr_shadow_name,
        }

    # ------------------------------------------------------------------
    # Actions
    # ------------------------------------------------------------------

    async def async_set_native_value(self, value: float) -> None:
        """Push the new setpoint to the AWS IoT shadow desired state."""
        _LOGGER.debug(
            "User set %s for zone %s to %.1f°C",
            self._key, self._zone_id, value,
        )
        await self.coordinator.async_publish_zone_setpoint(
            self._zone_id, self._key, value
        )

    @callback
    def _handle_coordinator_update(self) -> None:
        """Refresh HA state when coordinator data changes."""
        self.async_write_ha_state()
