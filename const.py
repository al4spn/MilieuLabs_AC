"""Constants for the Milieu Labs AC integration."""
from datetime import timedelta

DOMAIN = "milieulabs_ac"
API_URL = "https://telemetry-api.milieulabs.com.au/"
API_SystemID_URL = "https://69lfsbfsrb.execute-api.us-east-2.amazonaws.com/prod/all"
API_PROPERTIES_URL = "https://telemetry-api.milieulabs.com.au/V1/properties"
# SECURITY: These are vendor-issued public app identifiers (similar to an OAuth2 client_id).
# Do NOT commit this file to a public repository – doing so exposes your AWS infrastructure
# to enumeration and potential abuse. Consider loading from HA secrets or environment variables.
ClientId = "2radg6bqpp45h9gm2t360fh60h"
PoolId = "us-east-1_oOThwaWze"

# Update intervals
SCAN_INTERVAL = timedelta(minutes=5)  # Changed from 1 hour to 5 minutes
DEFAULT_TIMEOUT = 10

# Sensor keys
SENSOR_TEMPERATURE = "temperature"
SENSOR_HUMIDITY = "humidity"
SENSOR_PRESSURE = "pressure"
SENSOR_CO2 = "co2"
SENSOR_ZONE_TEMPERATURE = "zone_temperature"

# AWS IoT MQTT settings
# SECURITY: These endpoint and pool identifiers should not be committed to public repositories.
MQTT_ENDPOINT = "a26d847kr7mqsh-ats.iot.us-east-1.amazonaws.com"
AWS_REGION = "us-east-1"
COGNITO_IDENTITY_POOL_ID = "us-east-1:9c7e5cd7-84e3-4c02-a5d0-5b2a9d0876f3"
COGNITO_IDP = f"cognito-idp.{AWS_REGION}.amazonaws.com/{PoolId}"

# Possible shadow state keys for zone measured temperature (tried in order)
ZONE_TEMP_KEYS = ["measuredTemp", "temperature", "Temperature", "currentTemp", "temp", "MeasuredTemp"]