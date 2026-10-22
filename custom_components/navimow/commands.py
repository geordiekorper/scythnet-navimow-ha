"""Command submission and unsupported-operation reporting."""
from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Any, NoReturn

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import ConfigEntryAuthFailed, HomeAssistantError
from homeassistant.util import dt as dt_util
from mower_sdk.errors import MowerAuthRequiredError, MowerTransportError

if TYPE_CHECKING:
    from mower_sdk.models import MowerCommand

    from .coordinator import NavimowCoordinator

_LOGGER = logging.getLogger(__name__)

UNSUPPORTED_COMMANDS = {
    "set_blade_height": "Blade height adjustment is not supported by the Navimow REST API",
}


def refused_credentials(err: BaseException) -> bool:
    """Whether a failure means the credentials were refused, by the cloud
    (the SDK's MowerAuthRequiredError) or by Home Assistant's own token
    refresh (ConfigEntryAuthFailed): the one failure only the integration
    can act on, by starting reauthentication."""
    return isinstance(err, (MowerAuthRequiredError, ConfigEntryAuthFailed))


def start_reauth(hass: HomeAssistant, entry: ConfigEntry | None, what: str, err: BaseException) -> None:
    """Log refused credentials and start the entry's reauthentication flow."""
    _LOGGER.error("Navimow credentials refused during %s: %s", what, err)
    if entry is not None:
        entry.async_start_reauth(hass)


def reject_unsupported_command(command: str, device_id: str, **parameters: object) -> NoReturn:
    """Report a known unsupported operation without requiring a connection."""
    reason = UNSUPPORTED_COMMANDS[command]
    _LOGGER.warning(
        "Cannot submit %s command for device %s (parameters %s): %s",
        command, device_id, parameters, reason,
    )
    raise HomeAssistantError(f"{reason}; command '{command}' was not sent")


async def async_send_command(
    coordinator: NavimowCoordinator,
    device_id: str,
    command: MowerCommand,
) -> dict[str, Any]:
    """Submit a typed SDK command through the coordinator's client.

    The client asks the token provider for a fresh token first, keeps the
    receipt as ``client.last_receipt(device_id)`` and polls the status 5 s
    later whatever the reply, since a command whose reply was lost may still
    have acted, the cloud's status cache lags, and the mower may not report
    the transition over MQTT at once. The states that poll produces reach the
    coordinator through the client, so nothing is refreshed here.

    Returns the outcome: ``command``, ``status`` (the SDK's verdict on the
    reply, accepted, already_in_state or unknown; or unconfirmed when no
    usable reply came back, since the command may still have acted),
    ``error``, and ``sent_at`` / ``recorded_at``. A refused command raises
    HomeAssistantError; refused credentials also start reauthentication.
    """
    sent_at = dt_util.utcnow().isoformat()
    outcome: dict[str, Any] = {
        "command": command.value, "status": "unknown", "error": None,
        "sent_at": sent_at, "recorded_at": None,
    }
    what = f"the {command.value} command for device {device_id}"
    try:
        receipt = await coordinator.client.async_send_command(device_id, command)
    except ConfigEntryAuthFailed as err:
        # The token provider: Home Assistant could not refresh the token.
        start_reauth(coordinator.hass, coordinator.config_entry, what, err)
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
        start_reauth(coordinator.hass, coordinator.config_entry, what, err)
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
    return outcome
