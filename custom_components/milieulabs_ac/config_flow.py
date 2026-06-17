"""Config flow for Milieu Labs AC integration."""
import asyncio
import voluptuous as vol
import logging
import aiohttp
import async_timeout
import boto3
from typing import Any

from homeassistant import config_entries
from homeassistant.data_entry_flow import FlowResult

from .const import DOMAIN, ClientId, PoolId, API_PROPERTIES_URL
from pycognito import AWSSRP
from botocore.exceptions import ClientError

_LOGGER = logging.getLogger(__name__)

STEP_USER_LOGIN_SCHEMA = vol.Schema({
    vol.Required("username"): str,
    vol.Required("password"): str,
})


class MilieuLabsACConfigFlow(config_entries.ConfigFlow, domain=DOMAIN):
    """Handle a config flow for Milieu Labs AC."""

    VERSION = 1

    def __init__(self) -> None:
        """Initialize the config flow."""
        self._user_input: dict[str, Any] = {}
        self._hub_shadow_list: list[str] = []
        self._lvr_shadow_list: list[str] = []
        self._auth_result: dict[str, Any] = {}

    async def async_step_user(self, user_input: dict[str, Any] | None = None) -> FlowResult:
        """Handle the initial step."""
        errors = {}

        if user_input is not None:
            try:
                # Validate credentials and get auth tokens
                self._auth_result = await self._async_authenticate(
                    user_input["username"],
                    user_input["password"]
                )
                self._user_input = user_input
                
                # Fetch available devices
                await self._async_get_shadow_names()
                
                return await self.async_step_select_shadow()
                
            except InvalidAuth:
                errors["base"] = "invalid_auth"
            except CannotConnect:
                errors["base"] = "cannot_connect"
            except Exception as err:
                _LOGGER.exception("Unexpected error during authentication: %s", err)
                errors["base"] = "unknown"

        return self.async_show_form(
            step_id="user",
            data_schema=STEP_USER_LOGIN_SCHEMA,
            errors=errors
        )

    async def async_step_select_shadow(self, user_input: dict[str, Any] | None = None) -> FlowResult:
        """Handle device selection step."""
        if user_input is not None:
            # Create unique ID based on hub shadow name
            await self.async_set_unique_id(user_input["hub_shadow"])
            self._abort_if_unique_id_configured()

            return self.async_create_entry(
                title=f"Milieu Labs AC - {user_input['hub_shadow'][:8]}",
                data={
                    **self._auth_result,
                    "hub_shadow_name": user_input["hub_shadow"],
                    "lvr_shadow_name": user_input["lvr_shadow"],
                }
            )

        if not self._hub_shadow_list:
            return self.async_abort(reason="no_devices")

        return self.async_show_form(
            step_id="select_shadow",
            data_schema=vol.Schema({
                vol.Required("hub_shadow"): vol.In(self._hub_shadow_list),
                vol.Required("lvr_shadow"): vol.In(self._lvr_shadow_list),
            }),
            description_placeholders={
                "hub_count": str(len(self._hub_shadow_list)),
                "lvr_count": str(len(self._lvr_shadow_list)),
            }
        )

    async def _async_authenticate(self, username: str, password: str) -> dict[str, Any]:
        """Authenticate with Cognito and return tokens."""
        def authenticate_with_cognito():
            """Synchronous Cognito authentication."""
            try:
                client = boto3.client("cognito-idp", region_name='us-east-1')
                _LOGGER.debug("Creating AWS SRP authentication for user: %s", username)
                
                aws_srp = AWSSRP(
                    username=username,
                    password=password,
                    pool_id=PoolId,
                    client_id=ClientId,
                    client=client
                )
                
                auth_params = aws_srp.get_auth_params()
                
                # Initiate authentication
                response = client.initiate_auth(
                    ClientId=ClientId,
                    AuthFlow='USER_SRP_AUTH',
                    AuthParameters=auth_params
                )
                
                if response.get('ChallengeName') == 'PASSWORD_VERIFIER':
                    _LOGGER.debug("Processing password verifier challenge")
                    challenge_responses = aws_srp.process_challenge(
                        response['ChallengeParameters'],
                        auth_params
                    )
                    
                    # Respond to password challenge
                    response = client.respond_to_auth_challenge(
                        ClientId=ClientId,
                        ChallengeName='PASSWORD_VERIFIER',
                        ChallengeResponses=challenge_responses
                    )
                
                auth_result = response['AuthenticationResult']
                _LOGGER.debug("Authentication successful")
                
                return {
                    "id_token": auth_result['IdToken'],
                    "refresh_token": auth_result['RefreshToken']
                }
                
            except ClientError as e:
                error_code = e.response['Error']['Code']
                _LOGGER.error("Cognito error: %s", error_code)
                if error_code in ('NotAuthorizedException', 'UserNotFoundException'):
                    raise InvalidAuth from e
                raise CannotConnect from e
        
        try:
            return await self.hass.async_add_executor_job(authenticate_with_cognito)
        except Exception as err:
            _LOGGER.error("Authentication failed: %s", err)
            raise

    async def _async_get_shadow_names(self) -> None:
        """Fetch available hub and LVR shadow names."""
        headers = {
            "Authorization": f"Bearer {self._auth_result['id_token']}"
        }
        
        try:
            async with aiohttp.ClientSession() as session:
                async with async_timeout.timeout(10):
                    async with session.get(API_PROPERTIES_URL, headers=headers) as response:
                        if response.status == 200:
                            data = await response.json()
                            _LOGGER.debug("Fetching shadow names from API")
                            
                            for entry in data:
                                # Get hub shadow names
                                for hub in entry.get("hubs", []):
                                    if shadow_name := hub.get("shadowName"):
                                        self._hub_shadow_list.append(shadow_name)
                                        _LOGGER.debug("Found hub: %s", shadow_name)
                                
                                # Get LVR shadow names
                                for lvr in entry.get("lvrs", []):
                                    if shadow_name := lvr.get("shadowName"):
                                        self._lvr_shadow_list.append(shadow_name)
                                        _LOGGER.debug("Found LVR: %s", shadow_name)
                            
                            _LOGGER.info(
                                "Found %s hubs and %s LVRs", 
                                len(self._hub_shadow_list),
                                len(self._lvr_shadow_list)
                            )
                        else:
                            _LOGGER.error("API returned unexpected status: %s", response.status)
                            raise CannotConnect
                            
        except asyncio.TimeoutError as err:
            _LOGGER.error("Timeout fetching shadow names")
            raise CannotConnect from err
        except aiohttp.ClientError as err:
            _LOGGER.error("Network error fetching shadow names: %s", err)
            raise CannotConnect from err


    async def async_step_reauth(self, entry_data: dict[str, Any]) -> FlowResult:
        """Initiate re-authentication when the refresh token has expired."""
        return await self.async_step_reauth_confirm()

    async def async_step_reauth_confirm(self, user_input: dict[str, Any] | None = None) -> FlowResult:
        """Show login form and update the config entry with fresh tokens."""
        errors = {}

        if user_input is not None:
            try:
                auth_result = await self._async_authenticate(
                    user_input["username"],
                    user_input["password"],
                )
                reauth_entry = self._get_reauth_entry()
                return self.async_update_reload_and_abort(
                    reauth_entry,
                    data_updates=auth_result,
                    reason="reauth_successful",
                )
            except InvalidAuth:
                errors["base"] = "invalid_auth"
            except CannotConnect:
                errors["base"] = "cannot_connect"
            except Exception as err:
                _LOGGER.exception("Unexpected error during re-authentication: %s", err)
                errors["base"] = "unknown"

        return self.async_show_form(
            step_id="reauth_confirm",
            data_schema=STEP_USER_LOGIN_SCHEMA,
            errors=errors,
        )


class InvalidAuth(Exception):
    """Error to indicate invalid authentication."""


class CannotConnect(Exception):
    """Error to indicate we cannot connect."""