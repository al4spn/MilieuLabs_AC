"""The Milieu Labs AC integration."""
import logging
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.const import Platform
from homeassistant.exceptions import ConfigEntryNotReady
from homeassistant.helpers import config_validation as cv

from .const import DOMAIN
from .coordinator import MilieulabsacCoordinator

_LOGGER = logging.getLogger(__name__)

CONFIG_SCHEMA = cv.config_entry_only_config_schema(DOMAIN)
PLATFORMS = [Platform.SENSOR, Platform.CLIMATE]


async def async_setup(hass: HomeAssistant, config: dict) -> bool:
    """Set up the Milieu Labs AC component."""
    hass.data.setdefault(DOMAIN, {})
    return True


async def async_setup_entry(hass: HomeAssistant, config_entry: ConfigEntry) -> bool:
    """Set up Milieu Labs AC from a config entry."""
    _LOGGER.info(
        "Setting up Milieu Labs AC integration (entry_id: %s)", 
        config_entry.entry_id
    )

    # Extract configuration data
    id_token = config_entry.data.get("id_token")
    refresh_token = config_entry.data.get("refresh_token")
    hub_shadow_name = config_entry.data.get("hub_shadow_name")
    lvr_shadow_name = config_entry.data.get("lvr_shadow_name")

    # Validate required data
    if not all([id_token, refresh_token, hub_shadow_name]):
        _LOGGER.error("Missing required configuration data")
        return False

    try:
        # Set up the coordinator
        coordinator = MilieulabsacCoordinator(
            hass,
            hub_shadow_name=hub_shadow_name,
            lvr_shadow_name=lvr_shadow_name,
            id_token=id_token,
            refresh_token=refresh_token,
        )

        # Fetch initial data
        await coordinator.async_config_entry_first_refresh()
        _LOGGER.debug("First refresh complete for %s", hub_shadow_name)

        # Store coordinator and initialize sensor list
        hass.data[DOMAIN][config_entry.entry_id] = {
            "data": config_entry.data,
            "coordinator": coordinator,
            "sensors": []
        }

        # Set up platforms
        await hass.config_entries.async_forward_entry_setups(config_entry, PLATFORMS)

        # Log sensor setup
        sensors = hass.data[DOMAIN][config_entry.entry_id]["sensors"]
        if sensors:
            sensor_names = ", ".join([sensor.name for sensor in sensors])
            _LOGGER.info(
                "Successfully set up %s sensors for %s: %s", 
                len(sensors), 
                hub_shadow_name,
                sensor_names
            )
        else:
            _LOGGER.warning("No sensors were created for %s", hub_shadow_name)

        return True

    except Exception as err:
        _LOGGER.error(
            "Error setting up Milieu Labs AC: %s", 
            err, 
            exc_info=True
        )
        raise ConfigEntryNotReady from err


async def async_unload_entry(hass: HomeAssistant, config_entry: ConfigEntry) -> bool:
    """Unload a config entry."""
    _LOGGER.debug("Unloading Milieu Labs AC entry: %s", config_entry.entry_id)

    # Gracefully disconnect MQTT before platform teardown
    entry_data = hass.data.get(DOMAIN, {}).get(config_entry.entry_id, {})
    coordinator = entry_data.get("coordinator")
    if coordinator is not None:
        await coordinator.async_teardown_mqtt()

    unload_ok = await hass.config_entries.async_unload_platforms(
        config_entry, 
        PLATFORMS
    )
    
    if unload_ok:
        hass.data[DOMAIN].pop(config_entry.entry_id, None)
        _LOGGER.info("Successfully unloaded Milieu Labs AC integration")

    return unload_ok


async def async_reload_entry(hass: HomeAssistant, config_entry: ConfigEntry) -> None:
    """Reload config entry."""
    await async_unload_entry(hass, config_entry)
    await async_setup_entry(hass, config_entry)