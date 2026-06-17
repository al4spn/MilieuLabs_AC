"""Climate platform for Milieu Labs AC zones."""
import logging
import uuid as _uuid

from homeassistant.components.climate import (
    ClimateEntity,
    ClimateEntityFeature,
    HVACMode,
)
from homeassistant.const import UnitOfTemperature
from homeassistant.core import callback
from homeassistant.helpers.update_coordinator import CoordinatorEntity
from homeassistant.helpers.entity import DeviceInfo

from .const import DOMAIN

_LOGGER = logging.getLogger(__name__)

# Per-zone setpoint keys
ZONE_COOL_KEY       = "userSetCoolSetPoint_dC"      # USER_COOLING
ZONE_HEAT_KEY       = "userSetHeatSetPoint_dC"      # USER_HEATING
ZONE_DRY_KEY        = "userDrySetPoint_dC"          # USER_DRY
ZONE_RANGE_COOL_KEY = "autoRangeStartSetPoint_dC"   # range mode (HEAT_COOL, AUTO)
ZONE_RANGE_HEAT_KEY = "autoRangeEndSetPoint_dC"

# Shadow state values written to desired.Zone.zones.<id>.state
_SHADOW_STATE_ON = "ON"
_SHADOW_STATE_OFF = "OFF"

# Mapping: shadow userMode  ->  HA HVACMode
_USER_MODE_TO_HVAC = {
    "USER_OFF":            HVACMode.OFF,
    "USER_AUTO_HEAT_COOL": HVACMode.HEAT_COOL,
    "USER_COOLING":        HVACMode.COOL,
    "USER_HEATING":        HVACMode.HEAT,
    "USER_DRY":            HVACMode.DRY,
    "USER_FAN_ONLY":       HVACMode.FAN_ONLY,
    "AWAY":                HVACMode.AUTO,
}

# Reverse mapping: HA HVACMode  ->  shadow userMode
_HVAC_TO_USER_MODE = {v: k for k, v in _USER_MODE_TO_HVAC.items()}

_ALL_HVAC_MODES = list(_USER_MODE_TO_HVAC.values())

# Fan speed mappings: shadow value  ->  HA fan mode string
_FAN_MODE_TO_HA = {
    "FAN_SPEED_AUTO":   "auto",
    "FAN_SPEED_LOW":    "low",
    "FAN_SPEED_MID":    "medium",
    "FAN_SPEED_HIGH":   "high",
}
_HA_TO_FAN_MODE = {v: k for k, v in _FAN_MODE_TO_HA.items()}
_ALL_FAN_MODES = list(_FAN_MODE_TO_HA.values())

# Shadow field used for the user's chosen cool/heat/dry setpoints
USER_COOL_KEY       = "userSetCoolSetPoint_dC"
USER_HEAT_KEY       = "userSetHeatSetPoint_dC"
USER_DRY_KEY        = "userDrySetPoint_dC"
USER_RANGE_COOL_KEY = "autoRangeStartSetPoint_dC"  # range mode (HEAT_COOL, AUTO)
USER_RANGE_HEAT_KEY = "autoRangeEndSetPoint_dC"


async def async_setup_entry(hass, config_entry, async_add_entities) -> None:
    """Set up the main system climate entity, zone climate entities, and start MQTT."""
    _LOGGER.debug("Setting up climate for entry: %s", config_entry.entry_id)
    coordinator = hass.data[DOMAIN][config_entry.entry_id]["coordinator"]

    # Register the callback so newly discovered zones get entities created
    coordinator._async_add_zone_climate_entities = async_add_entities

    # Always create the main system-level climate entity
    entities: list = [MilieuACMainClimate(coordinator)]

    # Create entities for any zones already known (e.g. MQTT arrived first)
    entities += [
        MilieuACZoneClimate(coordinator, zone_id)
        for zone_id in coordinator.zone_data
    ]

    async_add_entities(entities, True)

    # Start MQTT (idempotent - guarded inside the method)
    await coordinator.async_setup_mqtt()


class MilieuACZoneClimate(CoordinatorEntity, ClimateEntity):
    """Climate entity for one AC zone.

    Exposes:
    - current_temperature      - live room temperature
    - hvac_mode                - OFF / HEAT_COOL (zone off / zone active)
    - target_temperature_low   - cool setpoint (autoRangeStartSetPoint_dC)
    - target_temperature_high  - heat setpoint (autoRangeEndSetPoint_dC)
    """

    _attr_temperature_unit = UnitOfTemperature.CELSIUS
    _attr_hvac_modes = [HVACMode.OFF, HVACMode.AUTO]
    _attr_target_temperature_step = 0.5
    _attr_min_temp = 16.0
    _attr_max_temp = 32.0
    _attr_has_entity_name = False

    def __init__(self, coordinator, zone_id: str) -> None:
        """Initialise the zone climate entity."""
        super().__init__(coordinator, context=f"climate_{zone_id}")
        self._zone_id = zone_id
        self._attr_unique_id = str(
            _uuid.uuid5(
                _uuid.NAMESPACE_DNS,
                f"{DOMAIN}_{coordinator.lvr_shadow_name}_climate_{zone_id}",
            )
        )
        self._attr_device_info = DeviceInfo(
            identifiers={(DOMAIN, coordinator.lvr_shadow_name)},
            name="Milieu Labs LVR",
            manufacturer="Milieu Labs",
            model="LVR",
            via_device=(DOMAIN, coordinator.hub_shadow_name),
        )

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    @property
    def _zone(self) -> dict:
        return self.coordinator.zone_data.get(self._zone_id, {})

    @property
    def _raw(self) -> dict:
        return self._zone.get("raw", {})

    @property
    def _system_mode(self) -> str:
        """Shortcut to the current system-wide userMode string."""
        return self.coordinator.system_user_mode

    @property
    def supported_features(self) -> ClimateEntityFeature:
        """Single target for HEAT/COOL/DRY; range for HEAT_COOL/AUTO; none for FAN_ONLY/OFF."""
        base = ClimateEntityFeature.TURN_ON | ClimateEntityFeature.TURN_OFF
        if self._system_mode in ("USER_HEATING", "USER_COOLING", "USER_DRY"):
            return base | ClimateEntityFeature.TARGET_TEMPERATURE
        if self._system_mode == "USER_FAN_ONLY":
            return base
        return base | ClimateEntityFeature.TARGET_TEMPERATURE_RANGE

    # ------------------------------------------------------------------
    # Identity
    # ------------------------------------------------------------------

    @property
    def name(self) -> str:
        return self._zone.get("name") or self._zone_id

    # ------------------------------------------------------------------
    # State
    # ------------------------------------------------------------------

    @property
    def available(self) -> bool:
        return self._zone_id in self.coordinator.zone_data

    @property
    def current_temperature(self) -> float | None:
        return self._zone.get("temperature")

    @property
    def hvac_mode(self) -> HVACMode:
        """Return OFF when the zone is inactive, HEAT_COOL when active."""
        state = self._zone.get("zone_state", "")
        if isinstance(state, str) and state.upper() == "OFF":
            return HVACMode.OFF
        return HVACMode.AUTO

    @property
    def target_temperature(self) -> float | None:
        """Single setpoint used when system is in HEAT, COOL, or DRY mode."""
        mode = self._system_mode
        if mode == "USER_HEATING":
            val = self._raw.get(ZONE_HEAT_KEY)
        elif mode == "USER_COOLING":
            val = self._raw.get(ZONE_COOL_KEY)
        elif mode == "USER_DRY":
            val = self._raw.get(ZONE_DRY_KEY)  # userDrySetPoint_dC
        else:
            return None  # range mode – HA uses low/high instead
        return round(val / 10.0, 1) if val is not None else None

    @property
    def target_temperature_low(self) -> float | None:
        """Cool setpoint for range (HEAT_COOL / AUTO) modes – uses autoRangeStartSetPoint_dC."""
        if self._system_mode in ("USER_HEATING", "USER_COOLING", "USER_DRY", "USER_FAN_ONLY", ""):
            return None
        val = self._raw.get(ZONE_RANGE_COOL_KEY)
        return round(val / 10.0, 1) if val is not None else None

    @property
    def target_temperature_high(self) -> float | None:
        """Heat setpoint for range (HEAT_COOL / AUTO) modes – uses autoRangeEndSetPoint_dC."""
        if self._system_mode in ("USER_HEATING", "USER_COOLING", "USER_DRY", "USER_FAN_ONLY", ""):
            return None
        val = self._raw.get(ZONE_RANGE_HEAT_KEY)
        return round(val / 10.0, 1) if val is not None else None

    @property
    def extra_state_attributes(self) -> dict:
        return {
            "zone_id": self._zone_id,
            "sensor_id": self._zone.get("sensor_id", ""),
            "zone_state": self._zone.get("zone_state", ""),
            "system_mode": self.coordinator.system_user_mode,
            "lvr_shadow_name": self.coordinator.lvr_shadow_name,
        }

    # ------------------------------------------------------------------
    # Actions
    # ------------------------------------------------------------------

    async def async_set_temperature(self, **kwargs) -> None:
        """Handle a setpoint change from the UI."""
        # Single-target modes (HEAT / COOL / DRY)
        temp = kwargs.get("temperature")
        if temp is not None:
            mode = self._system_mode
            if mode == "USER_HEATING":
                key = ZONE_HEAT_KEY
            elif mode == "USER_DRY":
                key = ZONE_DRY_KEY  # userDrySetPoint_dC
            else:
                key = ZONE_COOL_KEY
            await self.coordinator.async_publish_zone_setpoint(
                self._zone_id, key, float(temp)
            )
            return
        # Range mode (HEAT_COOL / AUTO) – writes autoRange keys
        low = kwargs.get("target_temp_low")
        high = kwargs.get("target_temp_high")
        if low is not None:
            await self.coordinator.async_publish_zone_setpoint(
                self._zone_id, ZONE_RANGE_COOL_KEY, float(low)
            )
        if high is not None:
            await self.coordinator.async_publish_zone_setpoint(
                self._zone_id, ZONE_RANGE_HEAT_KEY, float(high)
            )

    async def async_set_hvac_mode(self, hvac_mode: HVACMode) -> None:
        """Turn the zone on (HEAT_COOL) or off (OFF) via the shadow state field."""
        shadow_state = _SHADOW_STATE_OFF if hvac_mode == HVACMode.OFF else _SHADOW_STATE_ON
        _LOGGER.debug(
            "Setting zone %s hvac_mode=%s -> state=%s",
            self._zone_id, hvac_mode, shadow_state,
        )
        await self.coordinator.async_publish_zone_desired(
            self._zone_id, {"state": shadow_state}
        )

    async def async_toggle(self) -> None:
        """Toggle the zone between OFF and ON (AUTO)."""
        if self.hvac_mode == HVACMode.OFF:
            await self.async_set_hvac_mode(HVACMode.AUTO)
        else:
            await self.async_set_hvac_mode(HVACMode.OFF)

    @callback
    def _handle_coordinator_update(self) -> None:
        self.async_write_ha_state()


class MilieuACMainClimate(CoordinatorEntity, ClimateEntity):
    """System-level climate entity driven by Zone.user settings.

    Represents the whole-system AC config ("My AC").  Exposes:
    - hvac_mode               – from user.userMode
    - fan_mode                – from user.userFanSpeed
    - target_temperature_low  – user.userSetCoolSetPoint_dC
    - target_temperature_high – user.userSetHeatSetPoint_dC
    - current_temperature     – main hub sensor (coordinator.data)
    """

    _attr_temperature_unit = UnitOfTemperature.CELSIUS
    _attr_hvac_modes = _ALL_HVAC_MODES
    _attr_target_temperature_step = 0.5
    _attr_min_temp = 16.0
    _attr_max_temp = 32.0
    _attr_has_entity_name = False

    def __init__(self, coordinator) -> None:
        """Initialise the main system climate entity."""
        super().__init__(coordinator)
        self._attr_unique_id = str(
            _uuid.uuid5(
                _uuid.NAMESPACE_DNS,
                f"{DOMAIN}_{coordinator.lvr_shadow_name}_main_climate",
            )
        )
        self._attr_device_info = DeviceInfo(
            identifiers={(DOMAIN, coordinator.lvr_shadow_name)},
            name="Milieu Labs LVR",
            manufacturer="Milieu Labs",
            model="LVR",
            via_device=(DOMAIN, coordinator.hub_shadow_name),
        )
        # Remembers the last non-OFF mode so toggle can restore it
        self._last_hvac_mode: HVACMode = HVACMode.HEAT_COOL

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    @property
    def _user(self) -> dict:
        return self.coordinator.user_data

    @property
    def _user_mode(self) -> str:
        return self._user.get("userMode", "")

    @property
    def supported_features(self) -> ClimateEntityFeature:
        """Single target for HEAT/COOL/DRY; range for HEAT_COOL/AUTO; none for FAN_ONLY/OFF. Always includes FAN_MODE and TURN_ON/OFF."""
        base = ClimateEntityFeature.FAN_MODE | ClimateEntityFeature.TURN_ON | ClimateEntityFeature.TURN_OFF
        if self._user_mode in ("USER_HEATING", "USER_COOLING", "USER_DRY"):
            return base | ClimateEntityFeature.TARGET_TEMPERATURE
        if self._user_mode == "USER_FAN_ONLY":
            return base
        return base | ClimateEntityFeature.TARGET_TEMPERATURE_RANGE

    # ------------------------------------------------------------------
    # Identity
    # ------------------------------------------------------------------

    @property
    def name(self) -> str:
        return self._user.get("name") or "My AC"

    # ------------------------------------------------------------------
    # State
    # ------------------------------------------------------------------

    @property
    def available(self) -> bool:
        # Available as soon as we have any user_data from the shadow
        return bool(self.coordinator.user_data)

    @property
    def current_temperature(self) -> float | None:
        """Return the main hub sensor temperature."""
        return (self.coordinator.data or {}).get("temperature")

    @property
    def hvac_mode(self) -> HVACMode:
        return _USER_MODE_TO_HVAC.get(self._user.get("userMode", ""), HVACMode.OFF)

    @property
    def target_temperature(self) -> float | None:
        """Single setpoint for HEAT, COOL, or DRY modes."""
        mode = self._user_mode
        if mode == "USER_HEATING":
            val = self._user.get(USER_HEAT_KEY)
        elif mode == "USER_COOLING":
            val = self._user.get(USER_COOL_KEY)
        elif mode == "USER_DRY":
            val = self._user.get(USER_DRY_KEY)
        else:
            return None  # range mode or no-temp mode
        return round(val / 10.0, 1) if val is not None else None

    @property
    def target_temperature_low(self) -> float | None:
        """Cool setpoint for range (HEAT_COOL / AUTO) modes – autoRangeStartSetPoint_dC."""
        if self._user_mode in ("USER_HEATING", "USER_COOLING", "USER_DRY", "USER_FAN_ONLY", ""):
            return None
        val = self._user.get(USER_RANGE_COOL_KEY)
        return round(val / 10.0, 1) if val is not None else None

    @property
    def target_temperature_high(self) -> float | None:
        """Heat setpoint for range (HEAT_COOL / AUTO) modes – autoRangeEndSetPoint_dC."""
        if self._user_mode in ("USER_HEATING", "USER_COOLING", "USER_DRY", "USER_FAN_ONLY", ""):
            return None
        val = self._user.get(USER_RANGE_HEAT_KEY)
        return round(val / 10.0, 1) if val is not None else None

    @property
    def fan_modes(self) -> list[str]:
        """Return fan modes allowed for the current HVAC mode from shadow capabilities."""
        shadow_mode = _HVAC_TO_USER_MODE.get(self.hvac_mode)
        if shadow_mode:
            cap = self.coordinator.capabilities_data.get(shadow_mode, {})
            shadow_fans = cap.get("fanModes", [])
            if shadow_fans:
                ha_modes = [_FAN_MODE_TO_HA[f] for f in shadow_fans if f in _FAN_MODE_TO_HA]
                if ha_modes:
                    return ha_modes
        return _ALL_FAN_MODES

    @property
    def fan_mode(self) -> str | None:
        raw = self._user.get("fanMode") or self._user.get("userFanSpeed", "")
        return _FAN_MODE_TO_HA.get(raw)

    @property
    def extra_state_attributes(self) -> dict:
        u = self._user
        return {
            "user_mode":                  u.get("userMode", ""),
            "fan_mode_raw":               u.get("userFanSpeed") or u.get("fanMode", ""),
            "vane_position":              u.get("userVanePosition", ""),
            "away_mode_enabled":          u.get("awayModeEnabled"),
            "away_cooling_setpoint_c":    round(u["awaycoolingSetPoint_dC"] / 10.0, 1)
                                          if u.get("awaycoolingSetPoint_dC") is not None else None,
            "away_heating_setpoint_c":    round(u["awayheatingSetPoint_dC"] / 10.0, 1)
                                          if u.get("awayheatingSetPoint_dC") is not None else None,
            "standalone_mode_enabled":    u.get("standaloneModeEnabled"),
            "lockout_temp_enabled":       u.get("lockOutTemperatureEnabled"),
            "lvr_shadow_name":            self.coordinator.lvr_shadow_name,
        }

    # ------------------------------------------------------------------
    # Actions
    # ------------------------------------------------------------------

    async def async_set_hvac_mode(self, hvac_mode: HVACMode) -> None:
        """Publish userMode to Zone.user and all heads.

        If the current fan mode is not valid for the new HVAC mode, automatically
        switches to the first available fan mode for that mode.
        """
        user_mode = _HVAC_TO_USER_MODE.get(hvac_mode)
        if user_mode is None:
            _LOGGER.warning("No shadow userMode mapping for HVACMode %s", hvac_mode)
            return

        # Determine if current fan mode is valid for the new HVAC mode
        user_fields: dict = {"userMode": user_mode}
        cap = self.coordinator.capabilities_data.get(user_mode, {})
        allowed_shadow_fans = cap.get("fanModes", [])
        if allowed_shadow_fans:
            current_shadow_fan = (
                self.coordinator.user_data.get("userFanSpeed")
                or self.coordinator.user_data.get("fanMode", "")
            )
            if current_shadow_fan not in allowed_shadow_fans:
                new_shadow_fan = allowed_shadow_fans[0]
                user_fields["userFanSpeed"] = new_shadow_fan
                user_fields["fanMode"] = new_shadow_fan
                _LOGGER.debug(
                    "Fan mode '%s' not valid for %s; switching to '%s'",
                    current_shadow_fan, user_mode, new_shadow_fan,
                )

        # Publish to heads (propagates to zones) and user together
        await self.coordinator.async_publish_head_mode(user_mode)
        await self.coordinator.async_publish_user_settings(user_fields)

        # Remember last non-OFF mode for toggle
        if hvac_mode != HVACMode.OFF:
            self._last_hvac_mode = hvac_mode

    async def async_toggle(self) -> None:
        """Toggle between OFF and the last active HVAC mode."""
        if self.hvac_mode == HVACMode.OFF:
            await self.async_set_hvac_mode(self._last_hvac_mode)
        else:
            self._last_hvac_mode = self.hvac_mode
            await self.async_set_hvac_mode(HVACMode.OFF)

    async def async_set_fan_mode(self, fan_mode: str) -> None:
        """Publish userFanSpeed to Zone.user."""
        shadow_fan = _HA_TO_FAN_MODE.get(fan_mode)
        if shadow_fan is None:
            _LOGGER.warning("No shadow fan mapping for HA fan mode '%s'", fan_mode)
            return
        await self.coordinator.async_publish_user_settings(
            {"userFanSpeed": shadow_fan, "fanMode": shadow_fan}
        )

    async def async_set_temperature(self, **kwargs) -> None:
        """Publish cool/heat/dry setpoints to the user shadow."""
        fields: dict = {}
        # Single-target modes
        temp = kwargs.get("temperature")
        if temp is not None:
            mode = self._user_mode
            if mode == "USER_HEATING":
                fields[USER_HEAT_KEY] = round(float(temp) * 10)
            elif mode == "USER_DRY":
                fields[USER_DRY_KEY] = round(float(temp) * 10)
            else:  # USER_COOLING and fallback
                fields[USER_COOL_KEY] = round(float(temp) * 10)
        else:
            # Range mode (HEAT_COOL / AUTO) – writes autoRange keys
            low = kwargs.get("target_temp_low")
            high = kwargs.get("target_temp_high")
            if low is not None:
                fields[USER_RANGE_COOL_KEY] = round(float(low) * 10)
            if high is not None:
                fields[USER_RANGE_HEAT_KEY] = round(float(high) * 10)
        if fields:
            await self.coordinator.async_publish_user_settings(fields)

    @callback
    def _handle_coordinator_update(self) -> None:
        self.async_write_ha_state()
