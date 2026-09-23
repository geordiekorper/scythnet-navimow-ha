"""Binary sensor platform: the cloud connection's health (health.py)."""
from __future__ import annotations

from typing import Any

from homeassistant.components.binary_sensor import (
    BinarySensorDeviceClass,
    BinarySensorEntity,
)
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import EntityCategory
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity import DeviceInfo
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from .const import DOMAIN
from .health import CollectorHealth


async def async_setup_entry(
    hass: HomeAssistant,
    config_entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    data = hass.data[DOMAIN][config_entry.entry_id]
    async_add_entities(
        NavimowCloudConnected(data["health"], device) for device in data["devices"]
    )


def device_info(device: Any) -> DeviceInfo:
    """The mower's device entry, as the other platforms describe it."""
    return DeviceInfo(
        identifiers={(DOMAIN, device.id)},
        name=device.name,
        manufacturer="Navimow",
        model=device.model or "Unknown",
        sw_version=device.firmware_version or None,
        serial_number=device.serial_number or device.id,
    )


class NavimowCloudConnected(BinarySensorEntity):
    """On while the MQTT connection to the Navimow cloud is up.

    The connection belongs to the config entry, not to one mower, so every
    mower on the entry shows the same value.
    """

    _attr_has_entity_name = True
    _attr_name = "Cloud connected"
    _attr_device_class = BinarySensorDeviceClass.CONNECTIVITY
    _attr_entity_category = EntityCategory.DIAGNOSTIC
    _attr_should_poll = False

    def __init__(self, health: CollectorHealth, device: Any) -> None:
        self._health = health
        self._attr_unique_id = f"{DOMAIN}_{device.id}_cloud_connected"
        self._attr_device_info = device_info(device)

    async def async_added_to_hass(self) -> None:
        self.async_on_remove(self._health.async_add_listener(self.async_write_ha_state))

    @property
    def is_on(self) -> bool:
        return self._health.connected

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        return self._health.connection_attributes()
