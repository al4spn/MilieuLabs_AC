# Milieu Labs AC – Home Assistant Integration

A custom [Home Assistant](https://www.home-assistant.io/) integration for Milieu Labs ducted air conditioning systems. It connects to the Milieu Labs cloud (AWS Cognito + AWS IoT Device Shadow over MQTT) to expose your hub and zones as native Home Assistant entities — climate control per zone, system-wide mode control, hub environmental sensors, and per-zone setpoints.

## Features

- **Climate entities**
  - One climate entity per zone (on/off, target temperature / temperature range based on system mode).
  - A main system climate entity for setting the overall HVAC mode (`Off`, `Cool`, `Heat`, `Heat/Cool`, `Dry`, `Fan only`) and fan mode.
- **Sensors** (from the hub's onboard BME280 + air quality sensor)
  - Humidity
  - Pressure
  - CO2
- **Number entities**
  - Per-zone temperature setpoints.
- **Live updates** – zone and hub state is pushed via AWS IoT Device Shadow (MQTT), so changes made from the Milieu Labs app or device are reflected in Home Assistant in near real time, with no polling required.
- **Automatic re-authentication** – Cognito `id_token`s are refreshed transparently, and refreshed tokens are persisted to the config entry so reloads/restarts don't force you to log in again.

## Requirements

- Home Assistant 2024.x or newer (uses the modern `ConfigEntry`/`DataUpdateCoordinator` APIs, including reauth flows).
- A Milieu Labs account with at least one registered hub/LVR (Living Room) device.
- Outbound network access from Home Assistant to:
  - AWS Cognito (`cognito-idp` / `cognito-identity`, `us-east-1`)
  - The Milieu Labs telemetry API (`telemetry-api.milieulabs.com.au`)
  - AWS IoT Core MQTT-over-WebSocket endpoint (`*.iot.us-east-1.amazonaws.com`)

## Installation

### Manual

1. Copy this repository into your Home Assistant `config/custom_components` directory as `milieulabs_ac`:
   ```
   config/custom_components/milieulabs_ac/
   ```
2. Restart Home Assistant.
3. Go to **Settings → Devices & Services → Add Integration**, search for **Milieu Labs AC**, and follow the setup wizard.

### HACS (custom repository)

1. In HACS, add this repository as a custom integration repository.
2. Install **Milieu Labs AC** from HACS.
3. Restart Home Assistant.
4. Add the integration via **Settings → Devices & Services → Add Integration**.

## Configuration

Configuration is done entirely through the Home Assistant UI (no YAML required):

1. **Settings → Devices & Services → Add Integration → Milieu Labs AC**.
2. Enter your Milieu Labs account **username** and **password**. This authenticates against AWS Cognito using SRP (your password is never stored).
3. Select the **hub** and **living room (LVR)** shadow to set up.
4. The integration creates climate, sensor, and number entities for the selected hub/zones automatically as data arrives from the device shadow.

If your refresh token is ever revoked or expires, Home Assistant will prompt you to re-authenticate via the integration's reauth flow — just re-enter your credentials.

## Entities created

| Platform | Entity                          | Notes                                            |
|----------|----------------------------------|---------------------------------------------------|
| `climate`| Zone climate (one per zone)      | On/off, target temperature or range               |
| `climate`| System climate                   | Overall HVAC mode + fan mode                       |
| `sensor` | Humidity                         | From hub BME280                                    |
| `sensor` | Pressure                         | From hub BME280                                    |
| `sensor` | CO2                               | From hub air quality sensor                         |
| `number` | Zone setpoint (one per zone)      | Target temperature setpoint                         |

Zone entities are created dynamically the first time data for that zone is received from the device shadow.

## Troubleshooting

- **Repeated login prompts**: ensure Home Assistant can reach the AWS Cognito and IoT endpoints listed above; check the Home Assistant logs for `custom_components.milieulabs_ac` for Cognito error codes.
- **No entities created**: verify the selected hub/LVR shadow names are correct, and check logs for `Shadow GET rejected` errors.
- **MQTT connection drops every ~24h**: this is expected behaviour for AWS IoT SigV4 WebSocket connections; the integration automatically refreshes credentials and reconnects.

Enable debug logging for more detail:

```yaml
logger:
  default: info
  logs:
    custom_components.milieulabs_ac: debug
```

## Disclaimer

This is an unofficial, community-developed integration and is not affiliated with or endorsed by Milieu Labs. Use at your own risk.
