"""Coordinator for Milieu Labs AC integration."""
import logging
import asyncio
import boto3
import uuid as _uuid
from datetime import datetime, timedelta
from botocore.exceptions import ClientError
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator


class _TokenExpiredError(Exception):
    """Cognito refresh token has expired – user must re-authenticate."""
from homeassistant.core import HomeAssistant
from .const import (
    DOMAIN,
    ClientId,
    MQTT_ENDPOINT, AWS_REGION, COGNITO_IDENTITY_POOL_ID, COGNITO_IDP,
)

_LOGGER = logging.getLogger(__name__)


class MilieulabsacCoordinator(DataUpdateCoordinator):
    """Coordinator to manage data updates."""

    def __init__(
        self,
        hass: HomeAssistant,
        hub_shadow_name: str,
        lvr_shadow_name: str,
        id_token: str,
        refresh_token: str,
    ) -> None:
        """Initialize the coordinator."""
        super().__init__(
            hass,
            _LOGGER,
            name=DOMAIN,
        )
        self.hub_shadow_name = hub_shadow_name
        self.lvr_shadow_name = lvr_shadow_name
        self.id_token = id_token
        self.refresh_token = refresh_token

        # Zone temperature data keyed by zone ID
        self.zone_data: dict = {}
        # Head data keyed by head ID (e.g. HEAD_1) - carries userMode etc.
        # All zones share the same system-wide userMode; read via system_user_mode property.
        self.heads_data: dict = {}
        # System-level user settings from reported.Zone.user
        self.user_data: dict = {}        # Capabilities per userMode from reported.capabilities
        self.capabilities_data: dict = {}        # Hub shadow sensor readings (BME280, iAQ etc.)
        self.hub_shadow_data: dict = {}        # Callback set by climate platform to add new zone climate entities
        self._async_add_zone_climate_entities = None
        self._known_zone_ids: set = set()
        # MQTT internals
        self._mqtt_connection = None
        self._shadow_client = None

        _LOGGER.debug("Milieulabs Coordinator initialized")

    async def _async_update_data(self) -> dict:
        """No polling – all data arrives via MQTT shadow callbacks."""
        return {}

    # ------------------------------------------------------------------
    # Zone temperature via AWS IoT Shadow / MQTT
    # ------------------------------------------------------------------

    async def async_setup_mqtt(self) -> None:
        """Connect to AWS IoT and subscribe to LVR shadow for zone data."""
        if self._mqtt_connection is not None:
            _LOGGER.debug("MQTT already connected – skipping setup")
            return
        _LOGGER.debug(
            "Starting MQTT setup for LVR shadow: %s (endpoint: %s)",
            self.lvr_shadow_name,
            MQTT_ENDPOINT,
        )
        try:
            await self.hass.async_add_executor_job(self._setup_mqtt_sync)
            _LOGGER.info(
                "AWS IoT MQTT connected – subscribed to LVR shadow: %s",
                self.lvr_shadow_name,
            )
        except _TokenExpiredError:
            _LOGGER.warning(
                "Cognito refresh token expired – triggering re-authentication flow"
            )
            if self.config_entry is not None:
                self.config_entry.async_start_reauth(self.hass)
        except Exception as err:
            _LOGGER.error("Failed to start MQTT for zone temperatures: %s", err, exc_info=True)

    async def async_teardown_mqtt(self) -> None:
        """Disconnect MQTT cleanly on unload."""
        if self._mqtt_connection is not None:
            try:
                await self.hass.async_add_executor_job(self._teardown_mqtt_sync)
            except Exception as err:
                _LOGGER.warning("Error disconnecting MQTT: %s", err)
            finally:
                self._mqtt_connection = None
                self._shadow_client = None

    def _get_iot_credentials_sync(self) -> dict:
        """Refresh id_token if needed, then exchange for temporary AWS IoT credentials."""
        self._refresh_id_token_if_needed_sync()
        identity_client = boto3.client("cognito-identity", region_name=AWS_REGION)
        id_resp = identity_client.get_id(
            IdentityPoolId=COGNITO_IDENTITY_POOL_ID,
            Logins={COGNITO_IDP: self.id_token},
        )
        cred_resp = identity_client.get_credentials_for_identity(
            IdentityId=id_resp["IdentityId"],
            Logins={COGNITO_IDP: self.id_token},
        )
        return cred_resp["Credentials"]

    def _refresh_id_token_if_needed_sync(self) -> None:
        """Use the Cognito refresh token to obtain a fresh id_token.

        Called synchronously from the executor thread just before the IoT
        credential exchange so the id_token is always current.
        """
        try:
            client = boto3.client("cognito-idp", region_name="us-east-1")
            response = client.initiate_auth(
                ClientId=ClientId,
                AuthFlow="REFRESH_TOKEN_AUTH",
                AuthParameters={"REFRESH_TOKEN": self.refresh_token},
            )
            auth_result = response["AuthenticationResult"]
            self.id_token = auth_result["IdToken"]
            new_refresh_token = auth_result.get("RefreshToken")
            refresh_token_rotated = (
                new_refresh_token is not None and new_refresh_token != self.refresh_token
            )
            if refresh_token_rotated:
                self.refresh_token = new_refresh_token
            _LOGGER.debug("Cognito id_token refreshed successfully")
            self._persist_tokens(refresh_token_rotated)
        except ClientError as err:
            error_code = err.response.get("Error", {}).get("Code", "")
            if error_code == "NotAuthorizedException":
                _LOGGER.warning(
                    "Cognito refresh token has expired or been revoked – re-authentication required"
                )
                raise _TokenExpiredError from err
            _LOGGER.error("Failed to refresh Cognito id_token: %s", err)
            raise

    def _persist_tokens(self, refresh_token_rotated: bool) -> None:
        """Save the current id_token (and refresh_token if rotated) to the config entry.

        Called from the executor thread after a Cognito refresh, so the tokens
        survive integration reloads / HA restarts instead of only living on
        this coordinator instance.
        """
        if self.config_entry is None:
            return

        new_data = {**self.config_entry.data, "id_token": self.id_token}
        if refresh_token_rotated:
            new_data["refresh_token"] = self.refresh_token

        def _update_entry() -> None:
            self.hass.config_entries.async_update_entry(self.config_entry, data=new_data)

        self.hass.loop.call_soon_threadsafe(_update_entry)

    def _setup_mqtt_sync(self) -> None:
        """Synchronous MQTT / shadow setup – runs in HA executor thread."""
        from awscrt import auth, mqtt
        from awsiot import mqtt_connection_builder, iotshadow

        QOS = mqtt.QoS.AT_LEAST_ONCE

        creds = self._get_iot_credentials_sync()
        credentials_provider = auth.AwsCredentialsProvider.new_static(
            access_key_id=creds["AccessKeyId"],
            secret_access_key=creds["SecretKey"],
            session_token=creds["SessionToken"],
        )

        self._mqtt_connection = mqtt_connection_builder.websockets_with_default_aws_signing(
            endpoint=MQTT_ENDPOINT,
            region=AWS_REGION,
            credentials_provider=credentials_provider,
            client_id=f"ha-milieulabs-{_uuid.uuid4()}",
            on_connection_interrupted=self._on_connection_interrupted,
            on_connection_resumed=self._on_connection_resumed,
        )

        connect_future = self._mqtt_connection.connect()
        connect_future.result(30)
        _LOGGER.debug("MQTT connected to %s", MQTT_ENDPOINT)

        self._shadow_client = iotshadow.IotShadowClient(self._mqtt_connection)

        # Subscribe to shadow get/accepted
        get_accepted_future, _ = self._shadow_client.subscribe_to_get_shadow_accepted(
            request=iotshadow.GetShadowSubscriptionRequest(thing_name=self.lvr_shadow_name),
            qos=QOS,
            callback=self._on_shadow_response,
        )
        get_accepted_future.result(10)
        _LOGGER.debug("Subscribed to get_shadow_accepted for %s", self.lvr_shadow_name)

        # Subscribe to shadow get/rejected – tells us if the request failed
        get_rejected_future, _ = self._shadow_client.subscribe_to_get_shadow_rejected(
            request=iotshadow.GetShadowSubscriptionRequest(thing_name=self.lvr_shadow_name),
            qos=QOS,
            callback=self._on_shadow_rejected,
        )
        get_rejected_future.result(10)
        _LOGGER.debug("Subscribed to get_shadow_rejected for %s", self.lvr_shadow_name)

        # Subscribe to delta updates (desired != reported)
        update_delta_future, _ = self._shadow_client.subscribe_to_shadow_delta_updated_events(
            request=iotshadow.ShadowDeltaUpdatedSubscriptionRequest(thing_name=self.lvr_shadow_name),
            qos=QOS,
            callback=self._on_shadow_delta,
        )
        update_delta_future.result(10)
        _LOGGER.debug("Subscribed to shadow_delta_updated for %s", self.lvr_shadow_name)

        # Subscribe to update/accepted – fires whenever the shadow accepts any update
        # (both our desired publishes and device reported updates).
        update_accepted_future, _ = self._shadow_client.subscribe_to_update_shadow_accepted(
            request=iotshadow.UpdateShadowSubscriptionRequest(thing_name=self.lvr_shadow_name),
            qos=QOS,
            callback=self._on_shadow_updated,
        )
        update_accepted_future.result(10)
        _LOGGER.debug("Subscribed to update_shadow_accepted for %s", self.lvr_shadow_name)

        # Subscribe to update/rejected – fires when our desired-state publish is rejected.
        update_rejected_future, _ = self._shadow_client.subscribe_to_update_shadow_rejected(
            request=iotshadow.UpdateShadowSubscriptionRequest(thing_name=self.lvr_shadow_name),
            qos=QOS,
            callback=self._on_shadow_update_rejected,
        )
        update_rejected_future.result(10)
        _LOGGER.debug("Subscribed to update_shadow_rejected for %s", self.lvr_shadow_name)

        # Request the current LVR shadow state
        _LOGGER.debug("Publishing get_shadow request for %s", self.lvr_shadow_name)
        get_future = self._shadow_client.publish_get_shadow(
            request=iotshadow.GetShadowRequest(thing_name=self.lvr_shadow_name),
            qos=QOS,
        )
        get_future.result(10)
        _LOGGER.debug("get_shadow publish complete for %s", self.lvr_shadow_name)

        # ---- Hub shadow subscriptions ----
        hub_get_accepted_future, _ = self._shadow_client.subscribe_to_get_shadow_accepted(
            request=iotshadow.GetShadowSubscriptionRequest(thing_name=self.hub_shadow_name),
            qos=QOS,
            callback=self._on_hub_shadow_response,
        )
        hub_get_accepted_future.result(10)
        _LOGGER.debug("Subscribed to get_shadow_accepted for hub %s", self.hub_shadow_name)

        hub_update_accepted_future, _ = self._shadow_client.subscribe_to_update_shadow_accepted(
            request=iotshadow.UpdateShadowSubscriptionRequest(thing_name=self.hub_shadow_name),
            qos=QOS,
            callback=self._on_hub_shadow_updated,
        )
        hub_update_accepted_future.result(10)
        _LOGGER.debug("Subscribed to update_shadow_accepted for hub %s", self.hub_shadow_name)

        hub_delta_future, _ = self._shadow_client.subscribe_to_shadow_delta_updated_events(
            request=iotshadow.ShadowDeltaUpdatedSubscriptionRequest(thing_name=self.hub_shadow_name),
            qos=QOS,
            callback=self._on_hub_shadow_delta,
        )
        hub_delta_future.result(10)
        _LOGGER.debug("Subscribed to shadow_delta_updated for hub %s", self.hub_shadow_name)

        # Request the current hub shadow state
        _LOGGER.debug("Publishing get_shadow request for hub %s", self.hub_shadow_name)
        hub_get_future = self._shadow_client.publish_get_shadow(
            request=iotshadow.GetShadowRequest(thing_name=self.hub_shadow_name),
            qos=QOS,
        )
        hub_get_future.result(10)
        _LOGGER.debug("get_shadow publish complete for hub %s", self.hub_shadow_name)

    def _teardown_mqtt_sync(self) -> None:
        """Synchronously disconnect MQTT."""
        if self._mqtt_connection is not None:
            disconnect_future = self._mqtt_connection.disconnect()
            disconnect_future.result(10)

    def _on_connection_interrupted(self, connection, error, **kwargs) -> None:
        """Fired by the SDK when the MQTT connection drops.

        AWS IoT WebSocket connections using SigV4 are forcibly closed after
        ~24 hours because the Cognito Identity credentials embedded in the
        original WebSocket URL expire.  The SDK's built-in reconnect loop
        cannot re-auth, so we do a full teardown + re-setup with fresh tokens.
        """
        _LOGGER.warning(
            "MQTT connection interrupted (error=%s) – scheduling full reconnect", error
        )
        # Null out the handles so async_setup_mqtt will not skip setup
        self._mqtt_connection = None
        self._shadow_client = None
        coro = self._async_reconnect_mqtt()
        try:
            asyncio.run_coroutine_threadsafe(coro, self.hass.loop)
        except RuntimeError as err:
            _LOGGER.warning("Could not schedule MQTT reconnect: %s", err)
            coro.close()

    def _on_connection_resumed(self, connection, return_code, session_present, **kwargs) -> None:
        """Fired if the SDK's own reconnect somehow succeeds (unexpected for SigV4)."""
        _LOGGER.info(
            "MQTT connection resumed (return_code=%s session_present=%s)",
            return_code, session_present,
        )

    async def _async_reconnect_mqtt(self) -> None:
        """Wait briefly, then re-establish MQTT with freshly refreshed credentials."""
        import asyncio as _asyncio
        _LOGGER.info("Waiting 5 s before MQTT reconnect...")
        await _asyncio.sleep(5)
        try:
            await self.async_setup_mqtt()
            _LOGGER.info("MQTT reconnected successfully")
        except _TokenExpiredError:
            _LOGGER.warning(
                "Cognito refresh token expired during MQTT reconnect – triggering re-authentication"
            )
            if self.config_entry is not None:
                self.config_entry.async_start_reauth(self.hass)
        except Exception as err:
            _LOGGER.error("MQTT reconnect failed: %s", err, exc_info=True)

    # Shadow callbacks (called from MQTT thread)

    def _on_shadow_response(self, response) -> None:
        """Handle GetShadow accepted response – contains the full reported state."""
        try:
            _LOGGER.debug("Shadow GET accepted callback fired for %s", self.lvr_shadow_name)
            if response is None:
                _LOGGER.warning("Shadow response is None for %s", self.lvr_shadow_name)
                return
            if response.state is None:
                _LOGGER.warning("Shadow response.state is None for %s – shadow may be empty", self.lvr_shadow_name)
                return
            reported = response.state.reported or {}
            _LOGGER.debug("Shadow reported state for %s: %s", self.lvr_shadow_name, reported)
            self._process_zone_state(reported)
        except Exception as err:
            _LOGGER.error("Error processing shadow response: %s", err, exc_info=True)

    def _on_shadow_rejected(self, error) -> None:
        """Handle GetShadow rejected – logs reason so we know why no data arrives."""
        _LOGGER.error(
            "Shadow GET rejected for %s – code=%s message=%s",
            self.lvr_shadow_name,
            getattr(error, "code", "?"),
            getattr(error, "message", "?"),
        )

    def _on_shadow_updated(self, response) -> None:
        """Handle update_shadow_accepted – fired for any accepted shadow update.

        When we publish a desired-state command, AWS IoT returns update/accepted
        with state.desired set to our payload and state.reported absent.  Processing
        the desired payload here confirms the shadow recorded the command and keeps
        local state in sync without waiting for the device to report back.
        """
        try:
            _LOGGER.debug("Shadow update_accepted callback fired for %s", self.lvr_shadow_name)
            if response is None or response.state is None:
                return

            reported = response.state.reported
            if reported:
                _LOGGER.debug(
                    "Shadow update_accepted reported keys for %s: %s",
                    self.lvr_shadow_name, list(reported.keys()),
                )
                self._process_zone_state(reported)
            else:
                # No reported state – this is our own desired-state publish being
                # confirmed.  Apply the desired payload so the UI reflects the
                # accepted command even before the device echoes it back.
                desired = response.state.desired
                if desired:
                    _LOGGER.debug(
                        "Shadow update_accepted confirmed desired state for %s: keys=%s",
                        self.lvr_shadow_name, list(desired.keys()),
                    )
                    self._process_zone_state(desired)
                else:
                    _LOGGER.debug(
                        "update_shadow_accepted had no reported or desired state for %s",
                        self.lvr_shadow_name,
                    )
        except Exception as err:
            _LOGGER.error("Error processing shadow update: %s", err, exc_info=True)

    def _on_shadow_update_rejected(self, error) -> None:
        """Handle update_shadow_rejected – fired when a desired-state publish is rejected."""
        _LOGGER.error(
            "Shadow UPDATE rejected for %s – code=%s message=%s",
            self.lvr_shadow_name,
            getattr(error, "code", "?"),
            getattr(error, "message", "?"),
        )

    def _on_shadow_delta(self, delta) -> None:
        """Handle shadow delta update – partial desired/reported diff."""
        try:
            _LOGGER.debug("Shadow delta callback fired for %s", self.lvr_shadow_name)
            if delta is None or delta.state is None:
                _LOGGER.debug("Shadow delta is empty for %s", self.lvr_shadow_name)
                return
            _LOGGER.debug("Shadow delta state for %s: %s", self.lvr_shadow_name, delta.state)
            self._process_zone_state(delta.state)
        except Exception as err:
            _LOGGER.error("Error processing shadow delta: %s", err, exc_info=True)

    # Hub shadow callbacks

    def _on_hub_shadow_response(self, response) -> None:
        """Handle GetShadow accepted for the hub shadow."""
        try:
            if response is None or response.state is None:
                return
            reported = response.state.reported or {}
            _LOGGER.debug("Hub shadow GET reported keys: %s", list(reported.keys()))
            self._process_hub_state(reported)
        except Exception as err:
            _LOGGER.error("Error processing hub shadow response: %s", err, exc_info=True)

    def _on_hub_shadow_updated(self, response) -> None:
        """Handle update_shadow_accepted for the hub shadow."""
        try:
            if response is None or response.state is None:
                return
            reported = response.state.reported
            if not reported:
                return
            _LOGGER.debug("Hub shadow update_accepted keys: %s", list(reported.keys()))
            self._process_hub_state(reported)
        except Exception as err:
            _LOGGER.error("Error processing hub shadow update: %s", err, exc_info=True)

    def _on_hub_shadow_delta(self, delta) -> None:
        """Handle shadow delta for the hub shadow."""
        try:
            if delta is None or delta.state is None:
                return
            _LOGGER.debug("Hub shadow delta keys: %s", list(delta.state.keys()))
            self._process_hub_state(delta.state)
        except Exception as err:
            _LOGGER.error("Error processing hub shadow delta: %s", err, exc_info=True)

    def _process_hub_state(self, reported: dict) -> None:
        """Parse BME280 and iAQ readings from the hub shadow reported state."""
        updated = False

        bme = reported.get("BME280")
        if isinstance(bme, dict):
            if "Humidity" in bme:
                self.hub_shadow_data["humidity"] = bme["Humidity"]
                updated = True
            if "Pressure" in bme:
                self.hub_shadow_data["pressure"] = bme["Pressure"]
                updated = True
            if "Temperature" in bme:
                self.hub_shadow_data["temperature"] = bme["Temperature"]
                updated = True

        iaq = reported.get("iAQ")
        if isinstance(iaq, dict) and "CO2" in iaq:
            self.hub_shadow_data["co2"] = iaq["CO2"]
            updated = True

        if updated:
            _LOGGER.debug(
                "Hub shadow data updated: humidity=%s pressure=%s co2=%s",
                self.hub_shadow_data.get("humidity"),
                self.hub_shadow_data.get("pressure"),
                self.hub_shadow_data.get("co2"),
            )
            coro = self._async_notify_hub_sensors()
            try:
                asyncio.run_coroutine_threadsafe(coro, self.hass.loop)
            except RuntimeError as err:
                _LOGGER.warning("Could not schedule _async_notify_hub_sensors: %s", err)
                coro.close()

    async def _async_notify_hub_sensors(self) -> None:
        """Notify all listeners that hub shadow data has changed."""
        self.async_update_listeners()

    def _process_zone_state(self, reported: dict) -> None:
        """Parse zone temperatures from shadow state and schedule HA updates."""
        _LOGGER.debug(
            "Processing zone state for %s – top-level keys: %s",
            self.lvr_shadow_name,
            list(reported.keys()) if isinstance(reported, dict) else type(reported),
        )

        # Shadow structure:
        #   reported['Zone']['zones']          – zone config: name, sensor ID, state, head
        #   reported['Zone']['heads']          – head config: userMode, setpoints
        #   reported['ZoneStatus']['sensors']  – live sensor readings: roomTemp in deci-°C
        zone_config  = reported.get("Zone", {})
        zone_status  = reported.get("ZoneStatus", {})
        zones_dict   = zone_config.get("zones", {})
        heads_dict   = zone_config.get("heads", {})
        sensors_dict = zone_status.get("sensors", {})
        user_dict         = reported.get("user", {})  # top-level in shadow, not under Zone
        capabilities_dict = reported.get("capabilities", {})

        # Merge incoming capabilities
        user_updated = False
        if isinstance(capabilities_dict, dict) and capabilities_dict:
            self.capabilities_data = {**self.capabilities_data, **capabilities_dict}
            user_updated = True
            _LOGGER.debug(
                "Merged capabilities for %s: %s",
                self.lvr_shadow_name, list(capabilities_dict.keys()),
            )

        # Merge incoming system user settings (Zone.user)
        if isinstance(user_dict, dict) and user_dict:
            self.user_data = {**self.user_data, **user_dict}
            user_updated = True
            _LOGGER.debug(
                "Merged Zone.user settings for %s: %s",
                self.lvr_shadow_name, list(user_dict.keys()),
            )

        # Merge any incoming head data into the shared heads_data store
        for head_id, head_info in heads_dict.items():
            if isinstance(head_info, dict):
                self.heads_data[head_id] = {
                    **self.heads_data.get(head_id, {}),
                    **head_info,
                }

        # Sentinel used when a sensor slot is empty / not connected
        INVALID_TEMP_THRESHOLD = 1000  # deci-°C (= 100 °C – clearly unreal)

        def _resolve_temp(sensor_id: str) -> float | None:
            """Look up roomTemp for a sensor; returns °C or None."""
            sensor_data = sensors_dict.get(sensor_id, {})
            if not sensor_data:
                return None
            raw_temp = sensor_data.get("roomTemp")
            if raw_temp is None:
                return None
            if raw_temp < INVALID_TEMP_THRESHOLD:
                return round(raw_temp / 10.0, 1)
            return None  # invalid sentinel (e.g. 7405)

        updated = False
        new_zone_ids = []

        if zones_dict:
            # Full update – rebuild zone_data from Zone.zones + ZoneStatus.sensors
            _LOGGER.debug(
                "Found %d zone(s) and %d sensor(s) for %s",
                len(zones_dict), len(sensors_dict), self.lvr_shadow_name,
            )
            for zone_id, zone_info in zones_dict.items():
                if not isinstance(zone_info, dict):
                    _LOGGER.debug("Zone %s is not a dict – skipping", zone_id)
                    continue

                # Preserve previously known values for fields absent in a partial update
                existing   = self.zone_data.get(zone_id, {})
                zone_name  = zone_info.get("name") or existing.get("name") or zone_id
                sensor_id  = zone_info.get("sensor", "") or existing.get("sensor_id", "")
                # Only overwrite zone_state if the incoming payload explicitly includes it
                zone_state = zone_info.get("state") or existing.get("zone_state", "UNKNOWN")

                # If this partial update has no sensor readings, keep the
                # temperature we already have rather than overwriting with None.
                temperature = _resolve_temp(sensor_id)
                if temperature is None and not sensors_dict:
                    temperature = existing.get("temperature")

                # Merge incoming zone_info on top of existing raw so fields
                # absent from a partial update (e.g. setpoints) are not lost.
                merged_raw = {**existing.get("raw", {}), **zone_info}

                _LOGGER.debug(
                    "Zone %s ('%s'): sensor=%s temp=%s state=%s",
                    zone_id, zone_name, sensor_id, temperature, zone_state,
                )

                self.zone_data[zone_id] = {
                    "name": zone_name,
                    "temperature": temperature,
                    "sensor_id": sensor_id,
                    "zone_state": zone_state,
                    "raw": merged_raw,
                }
                updated = True

                if zone_id not in self._known_zone_ids:
                    self._known_zone_ids.add(zone_id)
                    new_zone_ids.append(zone_id)
                    _LOGGER.info(
                        "Discovered new zone: id=%s name='%s' sensor=%s temp=%s state=%s",
                        zone_id, zone_name, sensor_id, temperature, zone_state,
                    )

        elif sensors_dict and self.zone_data:
            # Partial update – only ZoneStatus arrived (e.g. a live temperature push).
            # Patch temperatures for all already-known zones.
            _LOGGER.debug(
                "Partial update: no Zone.zones, patching temperatures for %d known zone(s)",
                len(self.zone_data),
            )
            for zone_id, existing in self.zone_data.items():
                sensor_id = existing.get("sensor_id", "")
                temperature = _resolve_temp(sensor_id)
                if temperature is not None:
                    existing["temperature"] = temperature
                    updated = True
                    _LOGGER.debug(
                        "Patched temperature for zone %s ('%s'): %.1f°C",
                        zone_id, existing.get("name", zone_id), temperature,
                    )

        else:
            # Update contains neither Zone.zones nor ZoneStatus.sensors –
            # this is normal for shadow updates that only carry other keys
            # (e.g. temperatureControlStatus or user setpoints).
            if not user_updated:
                _LOGGER.debug(
                    "Shadow update for %s has no zone/sensor/user data – ignoring. "
                    "Top-level keys: %s",
                    self.lvr_shadow_name,
                    list(reported.keys()),
                )
                return
            _LOGGER.debug(
                "Shadow update for %s has only user/capabilities data – notifying listeners.",
                self.lvr_shadow_name,
            )

        _LOGGER.debug(
            "Zone state processing complete: %d known zone(s), %d new, for %s",
            len(self.zone_data), len(new_zone_ids), self.lvr_shadow_name,
        )

        if updated or user_updated:
            coro = self._async_notify_zones(new_zone_ids)
            try:
                asyncio.run_coroutine_threadsafe(coro, self.hass.loop)
            except RuntimeError as err:
                _LOGGER.warning("Could not schedule _async_notify_zones: %s", err)
                coro.close()

    async def _async_notify_zones(self, new_zone_ids: list) -> None:
        """Create climate entities for newly discovered zones and refresh existing ones."""
        _LOGGER.debug(
            "_async_notify_zones called: %d new zone(s), climate callback present=%s",
            len(new_zone_ids),
            self._async_add_zone_climate_entities is not None,
        )
        if new_zone_ids and self._async_add_zone_climate_entities is not None:
            from .climate import MilieuACZoneClimate
            new_entities = [
                MilieuACZoneClimate(self, zone_id)
                for zone_id in new_zone_ids
            ]
            self._async_add_zone_climate_entities(new_entities, True)
            _LOGGER.info(
                "Registered %d new zone climate entity(ies): %s",
                len(new_entities),
                [e.name for e in new_entities],
            )
        elif new_zone_ids and self._async_add_zone_climate_entities is None:
            _LOGGER.debug(
                "Climate callback not registered yet for zone(s) %s – "
                "entities will be created when climate platform sets up",
                new_zone_ids,
            )

        # Notify all existing zone climate entities to refresh state
        self.async_update_listeners()

    async def async_publish_zone_setpoint(
        self, zone_id: str, key: str, value_celsius: float
    ) -> None:
        """Publish a zone setpoint change to the AWS IoT shadow desired state.

        Args:
            zone_id:       Shadow zone key, e.g. ``'ZONE_1'``.
            key:           Setpoint field name, e.g. ``'userSetCoolSetPoint_dC'``.
            value_celsius: New setpoint value in °C (will be converted to deci-°C).
        """
        value_dc = round(value_celsius * 10)
        _LOGGER.debug(
            "Publishing shadow update: %s.%s = %d dC (%.1f °C) for thing %s",
            zone_id, key, value_dc, value_celsius, self.lvr_shadow_name,
        )

        def publish_sync() -> None:
            from awscrt import mqtt
            from awsiot import iotshadow

            future = self._shadow_client.publish_update_shadow(
                request=iotshadow.UpdateShadowRequest(
                    thing_name=self.lvr_shadow_name,
                    state=iotshadow.ShadowState(
                        desired={"Zone": {"zones": {zone_id: {key: value_dc}}}}
                    ),
                ),
                qos=mqtt.QoS.AT_LEAST_ONCE,
            )
            future.result(10)  # block up to 10 s for publish ACK

        await self.hass.async_add_executor_job(publish_sync)

        # Optimistically update local raw state so the UI reflects the change
        zone_raw = self.zone_data.get(zone_id, {}).get("raw", {})
        if isinstance(zone_raw, dict):
            zone_raw[key] = value_dc
        self.async_update_listeners()
        _LOGGER.info(
            "Shadow update published: zone=%s %s=%.1f °C (%d dC)",
            zone_id, key, value_celsius, value_dc,
        )

    async def async_publish_zone_desired(
        self, zone_id: str, fields: dict
    ) -> None:
        """Publish arbitrary desired zone fields to the AWS IoT shadow.

        Args:
            zone_id: Shadow zone key, e.g. ``'ZONE_1'``.
            fields:  Dict of raw field names/values to write into desired state,
                     e.g. ``{'state': 'OFF'}``.
        """
        _LOGGER.debug(
            "Publishing desired zone fields for %s/%s: %s",
            self.lvr_shadow_name, zone_id, fields,
        )

        def publish_sync() -> None:
            from awscrt import mqtt
            from awsiot import iotshadow

            future = self._shadow_client.publish_update_shadow(
                request=iotshadow.UpdateShadowRequest(
                    thing_name=self.lvr_shadow_name,
                    state=iotshadow.ShadowState(
                        desired={"Zone": {"zones": {zone_id: fields}}}
                    ),
                ),
                qos=mqtt.QoS.AT_LEAST_ONCE,
            )
            future.result(10)

        await self.hass.async_add_executor_job(publish_sync)

        # Optimistically update local zone data so the UI reflects the change
        if zone_id in self.zone_data:
            if "state" in fields:
                self.zone_data[zone_id]["zone_state"] = fields["state"]
            zone_raw = self.zone_data[zone_id].get("raw", {})
            if isinstance(zone_raw, dict):
                zone_raw.update(fields)
        self.async_update_listeners()
        _LOGGER.info(
            "Shadow desired published: zone=%s fields=%s",
            zone_id, fields,
        )

    @property
    def system_user_mode(self) -> str:
        """Return the current system-wide userMode.

        Prefers Zone.user.userMode (the authoritative user-facing value),
        falling back to the first head's userMode.
        """
        if isinstance(self.user_data, dict) and "userMode" in self.user_data:
            return self.user_data["userMode"]
        for head in self.heads_data.values():
            if isinstance(head, dict) and "userMode" in head:
                return head["userMode"]
        return ""

    async def async_publish_head_mode(self, user_mode: str) -> None:
        """Publish a userMode change to all heads in the AWS IoT shadow."""
        _LOGGER.debug(
            "Publishing head mode for %s: userMode=%s",
            self.lvr_shadow_name, user_mode,
        )
        heads_payload = {head_id: {"userMode": user_mode} for head_id in self.heads_data}
        if not heads_payload:
            _LOGGER.warning("No heads known yet - cannot publish userMode")
            return

        def publish_sync() -> None:
            from awscrt import mqtt
            from awsiot import iotshadow

            future = self._shadow_client.publish_update_shadow(
                request=iotshadow.UpdateShadowRequest(
                    thing_name=self.lvr_shadow_name,
                    state=iotshadow.ShadowState(
                        desired={"Zone": {"heads": heads_payload}}
                    ),
                ),
                qos=mqtt.QoS.AT_LEAST_ONCE,
            )
            future.result(10)

        await self.hass.async_add_executor_job(publish_sync)

        # Optimistically update all heads and user_data
        for head in self.heads_data.values():
            if isinstance(head, dict):
                head["userMode"] = user_mode
        self.user_data["userMode"] = user_mode
        self.async_update_listeners()
        _LOGGER.info("Head mode published: userMode=%s", user_mode)

    async def async_publish_user_settings(self, fields: dict) -> None:
        """Publish arbitrary fields to desired.Zone.user in the AWS IoT shadow.

        Args:
            fields: Dict of field names/values, e.g.
                    ``{'userSetCoolSetPoint_dC': 240, 'userFanSpeed': 'FAN_SPEED_LOW'}``.
        """
        _LOGGER.debug(
            "Publishing user settings for %s: %s",
            self.lvr_shadow_name, fields,
        )

        def publish_sync() -> None:
            from awscrt import mqtt
            from awsiot import iotshadow

            future = self._shadow_client.publish_update_shadow(
                request=iotshadow.UpdateShadowRequest(
                    thing_name=self.lvr_shadow_name,
                    state=iotshadow.ShadowState(
                        desired={"user": fields}
                    ),
                ),
                qos=mqtt.QoS.AT_LEAST_ONCE,
            )
            future.result(10)

        await self.hass.async_add_executor_job(publish_sync)

        # Optimistically update local user_data so UI reflects the change
        self.user_data.update(fields)
        self.async_update_listeners()
        _LOGGER.info("User settings published: %s", fields)
