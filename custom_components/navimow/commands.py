"""Command submission and unsupported-operation reporting."""
from __future__ import annotations

import asyncio
import logging
from typing import TYPE_CHECKING, Any, NoReturn

import aiohttp

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


def classify_reply(data: Any) -> str:
    """The vendor's verdict on a submitted command, read as Scythnet does.

    ``data`` is what MowerAPI.async_send_command returns. A command the mower
    was already carrying out comes back as an ERROR with errorCode
    alreadyInState, which the SDK lets through; SUCCESS means accepted; a
    reply with neither is unknown.
    """
    payload = data.get("payload") if isinstance(data, dict) else None
    results = payload.get("commands") if isinstance(payload, dict) else None
    status = "unknown"
    for result in results if isinstance(results, list) else []:
        if not isinstance(result, dict):
            continue
        if result.get("status") == "ERROR" and result.get("errorCode") == "alreadyInState":
            status = "already_in_state"
        elif result.get("status") == "SUCCESS" and status == "unknown":
            status = "accepted"
    return status


def _no_reply(err: BaseException) -> bool:
    """Whether a submission failed without any reply from the cloud (the
    request may still have reached it), as opposed to being refused."""
    for candidate in (err, err.__cause__):
        if isinstance(candidate, (aiohttp.ClientError, asyncio.TimeoutError)):
            return True
    return False


async def async_send_command(
    api: MowerAPI,
    coordinator: NavimowCoordinator,
    device_id: str,
    command: MowerCommand,
) -> dict[str, Any]:
    """Submit a typed SDK command; follow-up refresh is best effort.

    Returns the outcome: ``command``, ``status`` (accepted, already_in_state,
    unknown, or unconfirmed when no reply came back, since the command may
    still have acted), ``error``, and ``sent_at`` / ``recorded_at``. A
    refused command raises HomeAssistantError.
    """
    sent_at = dt_util.utcnow().isoformat()
    outcome: dict[str, Any] = {
        "command": command.value, "status": "unknown", "error": None,
        "sent_at": sent_at, "recorded_at": None,
    }
    try:
        await coordinator._async_ensure_valid_token()
        try:
            reply = await api.async_send_command(device_id, command)
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
    except Exception as err:
        if _no_reply(err):
            _LOGGER.warning(
                "No reply to %s command for device %s; it may still act: %s",
                command.value, device_id, err,
            )
            outcome.update(status="unconfirmed", error=str(err) or type(err).__name__,
                           recorded_at=dt_util.utcnow().isoformat())
            return outcome
        _LOGGER.error("Failed to submit %s command for device %s: %s", command.value, device_id, err)
        if isinstance(err, HomeAssistantError):
            raise
        raise HomeAssistantError(f"Navimow {command.value} failed: {err}") from err

    outcome.update(status=classify_reply(reply), recorded_at=dt_util.utcnow().isoformat())
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
