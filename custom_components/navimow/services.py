"""Services for Navimow integration."""

import logging

import voluptuous as vol

from homeassistant.core import HomeAssistant, ServiceCall
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers import config_validation as cv, device_registry as dr

from mower_sdk.api import MowerAPI
from mower_sdk.models import MowerCommand

from .const import DOMAIN

_LOGGER = logging.getLogger(__name__)

COMMAND_SERVICES = {"resume": MowerCommand.RESUME, "stop": MowerCommand.STOP}
COMMAND_SCHEMA = vol.Schema({vol.Required("device_id"): cv.string})

SERVICE_SET_BLADE_HEIGHT = "set_blade_height"

SERVICE_SCHEMA_SET_BLADE_HEIGHT = vol.Schema(
    {
        vol.Required("device_id"): cv.string,
        vol.Required("height"): vol.Coerce(int),
    }
)


def async_setup_services(hass: HomeAssistant, _api: MowerAPI) -> None:
    async def _handle_command(call: ServiceCall) -> None:
        # Resolve HA's device registry ID, never accept an arbitrary cloud ID.
        device = dr.async_get(hass).async_get(call.data["device_id"])
        if device is None:
            raise HomeAssistantError("Select a registered Navimow device")
        vendor_ids = {identifier for domain, identifier in device.identifiers if domain == DOMAIN}
        matches = [
            (vendor_id, data, coordinator)
            for data in hass.data.get(DOMAIN, {}).values()
            if isinstance(data, dict) and not data.get("unload_flag", [False])[0]
            for vendor_id, coordinator in data.get("coordinators", {}).items()
            if vendor_id in vendor_ids
        ]
        if len(matches) != 1:
            raise HomeAssistantError("The selected Navimow device is not loaded or is ambiguous")
        vendor_id, data, coordinator = matches[0]
        await coordinator._async_ensure_valid_token()
        try:
            await data["api"].async_send_command(vendor_id, COMMAND_SERVICES[call.service])
        except HomeAssistantError:
            raise
        except Exception as err:
            raise HomeAssistantError(f"Navimow {call.service} failed: {err}") from err
        await coordinator.async_request_refresh()

    for service in COMMAND_SERVICES:
        if not hass.services.has_service(DOMAIN, service):
            hass.services.async_register(DOMAIN, service, _handle_command, schema=COMMAND_SCHEMA)

    async def _handle_set_blade_height(call: ServiceCall) -> None:
        device_id = call.data["device_id"]
        height = call.data["height"]
        _LOGGER.warning(
            "Blade height change not supported via REST API (device %s, height %s)",
            device_id,
            height,
        )
        raise HomeAssistantError(
            "当前 REST API 不支持设置割草高度，服务未执行"
        )

    hass.services.async_register(
        DOMAIN,
        SERVICE_SET_BLADE_HEIGHT,
        _handle_set_blade_height,
        schema=SERVICE_SCHEMA_SET_BLADE_HEIGHT,
    )


def async_unload_services(hass: HomeAssistant) -> None:
    """Remove domain actions after the last integration entry unloads."""
    if hass.data.get(DOMAIN):
        return
    for service in (*COMMAND_SERVICES, SERVICE_SET_BLADE_HEIGHT):
        hass.services.async_remove(DOMAIN, service)
