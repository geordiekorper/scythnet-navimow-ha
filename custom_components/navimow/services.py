"""Services for Navimow integration."""

import voluptuous as vol
from homeassistant.core import HomeAssistant, ServiceCall
from homeassistant.helpers import config_validation as cv
from homeassistant.helpers import service

from .commands import reject_unsupported_command
from .const import DOMAIN

# Home Assistant's lawn_mower domain provides start_mowing, pause, and dock
# through lawn_mower.py. Register resume and stop here because the domain
# has no standard actions for them.
COMMAND_SERVICES = {"resume": "async_resume", "stop": "async_stop"}

SERVICE_SET_BLADE_HEIGHT = "set_blade_height"

SERVICE_SCHEMA_SET_BLADE_HEIGHT = vol.Schema(
    {
        vol.Required("device_id"): cv.string,
        vol.Required("height"): vol.Coerce(int),
    }
)


def async_setup_services(hass: HomeAssistant) -> None:
    for name, method in COMMAND_SERVICES.items():
        if not hass.services.has_service(DOMAIN, name):
            service.async_register_platform_entity_service(
                hass, DOMAIN, name, entity_domain="lawn_mower", schema=None, func=method
            )

    async def _handle_set_blade_height(call: ServiceCall) -> None:
        reject_unsupported_command(
            SERVICE_SET_BLADE_HEIGHT, call.data["device_id"], height=call.data["height"]
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
    for name in (*COMMAND_SERVICES, SERVICE_SET_BLADE_HEIGHT):
        hass.services.async_remove(DOMAIN, name)
