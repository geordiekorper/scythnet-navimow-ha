"""Lawn mower platform for Navimow integration."""
from typing import Any

from homeassistant.components.lawn_mower import (
    LawnMowerActivity,
    LawnMowerEntity,
    LawnMowerEntityFeature,
)
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.update_coordinator import CoordinatorEntity
from mower_sdk.models import MowerCommand, MowerError, MowerStatus
from mower_sdk.navimow_client import MowerState

from .commands import async_send_command
from .const import DOMAIN, MOWER_STATUS_TO_ACTIVITY
from .coordinator import NavimowCoordinator
from .entity import device_info


async def async_setup_entry(
    hass: HomeAssistant,
    config_entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Set up lawn mower entities from a config entry."""
    coordinators: dict[str, NavimowCoordinator] = hass.data[DOMAIN][config_entry.entry_id]["coordinators"]
    async_add_entities(NavimowLawnMower(coordinator) for coordinator in coordinators.values())


class NavimowLawnMower(CoordinatorEntity[NavimowCoordinator], LawnMowerEntity):
    """Representation of a Navimow lawn mower."""

    _attr_supported_features = (
        LawnMowerEntityFeature.START_MOWING
        | LawnMowerEntityFeature.PAUSE
        | LawnMowerEntityFeature.DOCK
    )

    def __init__(self, coordinator: NavimowCoordinator) -> None:
        """Initialize the lawn mower entity."""
        super().__init__(coordinator)
        device = coordinator.device
        self._device_id = device.id
        self._attr_name = device.name
        self._attr_unique_id = f"{DOMAIN}_{device.id}"
        self._attr_device_info = device_info(device)

    @property
    def available(self) -> bool:
        """Keep entity available as long as cached state exists.

        Broker-initiated MQTT disconnects (with paho auto-reconnect) are
        transient; the entity should not flip to unavailable during the
        brief reconnection window.
        """
        if self.coordinator.get_device_state() is not None:
            return True
        return super().available

    @property
    def activity(self) -> LawnMowerActivity:
        """Return the current activity of the lawn mower."""
        state = self.coordinator.get_device_state()
        if not state:
            return None
        activity = MOWER_STATUS_TO_ACTIVITY.get(state.status.value)
        if activity is None:
            return None
        return LawnMowerActivity(activity)

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        """Return extra state attributes."""
        state: MowerState | None = self.coordinator.get_device_state()
        attrs = self.coordinator.get_device_attributes()
        if not state:
            return {}
        # The exact state: the SDK's name, or the cloud's own text for one the
        # SDK cannot name (isLifted was seen), as before the client.
        status = state.raw_state if state.status is MowerStatus.UNKNOWN else state.status.value
        attributes: dict[str, Any] = {
            "battery": state.battery,
            "status": status,
        }
        # The rest of the observation: the state message, or the REST status
        # when that is what the state was built from.
        source = state.message if state.message is not None else state.rest_status
        signal_strength = getattr(source, "signal_strength", None)
        if signal_strength is not None:
            attributes["signal_strength"] = signal_strength
        position = getattr(source, "position", None)
        if position:
            attributes["position"] = position
        if state.message is not None:
            if state.message.error:
                attributes["error"] = state.message.error
            if state.message.metrics:
                attributes["metrics"] = state.message.metrics
        elif state.error_code is not MowerError.NONE:
            attributes["error"] = {"code": state.error_code.value, "message": state.error_message}
        if attrs:
            attributes["attributes"] = attrs.attributes
        return attributes

    async def async_start_mowing(self) -> dict[str, Any]:
        """Start mowing."""
        return await async_send_command(self.coordinator, self._device_id, MowerCommand.START)

    async def async_pause(self) -> dict[str, Any]:
        """Pause mowing."""
        return await async_send_command(self.coordinator, self._device_id, MowerCommand.PAUSE)

    async def async_dock(self) -> dict[str, Any]:
        """Dock the mower."""
        return await async_send_command(self.coordinator, self._device_id, MowerCommand.DOCK)

    async def async_resume(self) -> dict[str, Any]:
        """Resume mowing."""
        return await async_send_command(self.coordinator, self._device_id, MowerCommand.RESUME)

    async def async_navimow_command(self, command: str) -> dict[str, Any]:
        """Any command by name (navimow.command), with its outcome."""
        return await async_send_command(self.coordinator, self._device_id, MowerCommand(command))

    async def async_stop(self) -> dict[str, Any]:
        """Pause the task using Navimow's stop command."""
        return await async_send_command(self.coordinator, self._device_id, MowerCommand.STOP)
