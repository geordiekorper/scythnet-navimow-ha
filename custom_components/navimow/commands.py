"""Command submission and unsupported-operation reporting."""
from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Any, NoReturn

from homeassistant.exceptions import ConfigEntryAuthFailed, HomeAssistantError
from homeassistant.util import dt as dt_util

from .const import COMMAND_POLL_DELAY

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
) -> dict[str, Any]:
    """Submit a typed SDK command; follow-up refresh is best effort.

    Returns the outcome: ``command``, ``status`` (the SDK's verdict on the
    reply, accepted, already_in_state or unknown; or unconfirmed when no
    usable reply came back, since the command may still have acted),
    ``error``, and ``sent_at`` / ``recorded_at``. A refused command raises
    HomeAssistantError; refused credentials also start reauthentication.
    """
    # Imported here, not at module level: services.py imports this module,
    # and the package's start-up check must be able to run first.
    from mower_sdk.errors import MowerAuthRequiredError, MowerTransportError

    sent_at = dt_util.utcnow().isoformat()
    outcome: dict[str, Any] = {
        "command": command.value, "status": "unknown", "error": None,
        "sent_at": sent_at, "recorded_at": None,
    }
    try:
        await coordinator._async_ensure_valid_token()
        try:
            receipt = await api.async_send_command_receipt(device_id, command)
        finally:
            # Whatever the reply, check the effect soon: a command whose reply
            # was lost may still have acted, the cloud's status cache lags,
            # and the mower may not report the transition over MQTT at once.
            poller = getattr(coordinator, "rest_poller", None)
            if poller is not None:
                poller.async_request_poll(COMMAND_POLL_DELAY)
    except ConfigEntryAuthFailed:
        _LOGGER.error("Authentication required for %s command on device %s", command.value, device_id)
        if coordinator.config_entry is not None:
            coordinator.config_entry.async_start_reauth(coordinator.hass)
        raise
    except MowerTransportError as err:
        # A timeout, a connection error, an HTTP 5xx, a terminal redirect or
        # an unreadable reply: the cloud may still have carried it out.
        _LOGGER.warning(
            "No reply to %s command for device %s; it may still act: %s",
            command.value, device_id, err,
        )
        outcome.update(status="unconfirmed", error=str(err) or type(err).__name__,
                       recorded_at=dt_util.utcnow().isoformat())
        return outcome
    except MowerAuthRequiredError as err:
        _LOGGER.error(
            "Credentials refused for %s command on device %s: %s", command.value, device_id, err
        )
        if coordinator.config_entry is not None:
            coordinator.config_entry.async_start_reauth(coordinator.hass)
        raise HomeAssistantError(f"Navimow {command.value} failed: {err}") from err
    except Exception as err:
        _LOGGER.error("Failed to submit %s command for device %s: %s", command.value, device_id, err)
        if isinstance(err, HomeAssistantError):
            raise
        raise HomeAssistantError(f"Navimow {command.value} failed: {err}") from err

    outcome.update(status=receipt.verdict.value, recorded_at=dt_util.utcnow().isoformat())
    _LOGGER.info(
        "Submitted %s command for device %s (%s)", command.value, device_id, outcome["status"]
    )
    try:
        await coordinator.async_request_refresh()
    except Exception as err:  # noqa: BLE001 - refresh must not fail a submitted command
        _LOGGER.warning(
            "State refresh failed after %s command for device %s: %s",
            command.value, device_id, err,
        )
    return outcome
