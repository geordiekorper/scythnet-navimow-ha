"""Shared entity helpers."""
from __future__ import annotations

from typing import Any

from homeassistant.helpers.entity import DeviceInfo

from .const import DOMAIN


def device_info(device: Any) -> DeviceInfo:
    """The mower's device entry, described the same way by every platform."""
    return DeviceInfo(
        identifiers={(DOMAIN, device.id)},
        name=device.name,
        manufacturer="Navimow",
        model=device.model or "Unknown",
        sw_version=device.firmware_version or None,
        serial_number=device.serial_number or device.id,
    )
