"""Command submission and unsupported-operation reporting."""
from __future__ import annotations

import logging
from typing import TYPE_CHECKING, NoReturn

from homeassistant.exceptions import ConfigEntryAuthFailed, HomeAssistantError

if TYPE_CHECKING:
    from mower_sdk.api import MowerAPI
    from mower_sdk.models import MowerCommand

    from .coordinator import NavimowCoordinator

_LOGGER = logging.getLogger(__name__)

UNSUPPORTED_COMMANDS = {
    "set_blade_height": "Blade height adjustment is not supported by the Navimow REST API",
}


def reject_unsupported_command(command: str, device_id: str, **parameters: object) -> NoReturn:
    """Report a known unsupported operation without requiring a connection."""
    reason = UNSUPPORTED_COMMANDS[command]
    _LOGGER.warning(
        "Cannot submit %s command for device %s (parameters %s): %s",
        command, device_id, parameters, reason,
    )
    raise HomeAssistantError(f"{reason}; command '{command}' was not sent")


async def async_send_command(
    api: MowerAPI,
    coordinator: NavimowCoordinator,
    device_id: str,
    command: MowerCommand,
) -> None:
    """Submit a typed SDK command; follow-up refresh is best effort."""
    try:
        await coordinator._async_ensure_valid_token()
        await api.async_send_command(device_id, command)
    except ConfigEntryAuthFailed:
        _LOGGER.error("Authentication required for %s command on device %s", command.value, device_id)
        if coordinator.config_entry is not None:
            coordinator.config_entry.async_start_reauth(coordinator.hass)
        raise
    except Exception as err:
        _LOGGER.error("Failed to submit %s command for device %s: %s", command.value, device_id, err)
        if isinstance(err, HomeAssistantError):
            raise
        raise HomeAssistantError(f"Navimow {command.value} failed: {err}") from err

    _LOGGER.info("Submitted %s command for device %s", command.value, device_id)
    try:
        await coordinator.async_request_refresh()
    except Exception as err:  # noqa: BLE001 - refresh must not fail a submitted command
        _LOGGER.warning(
            "State refresh failed after %s command for device %s: %s",
            command.value, device_id, err,
        )
