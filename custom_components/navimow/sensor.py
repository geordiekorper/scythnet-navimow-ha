"""Sensor platform for Navimow integration."""
from __future__ import annotations

import math
import time
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Callable

from homeassistant.components.sensor import (
    RestoreSensor,
    SensorDeviceClass,
    SensorEntity,
    SensorEntityDescription,
    SensorStateClass,
)
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import PERCENTAGE, EntityCategory
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.event import async_call_later
from homeassistant.helpers.entity import DeviceInfo
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.restore_state import RestoreEntity
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from .const import DOMAIN, LAST_MESSAGE_RESOLUTION
from .coordinator import NavimowCoordinator
from .entity import device_info
from .health import CollectorHealth
from .location import (
    POSE_SOURCE,
    progress_percent,
    restore_location_groups,
    target_zone,
)


@dataclass(frozen=True, kw_only=True)
class NavimowSensorEntityDescription(SensorEntityDescription):
    """Describes Navimow sensor entity."""

    value_fn: Callable[[NavimowCoordinator], Any]


SENSOR_DESCRIPTIONS: tuple[NavimowSensorEntityDescription, ...] = (
    NavimowSensorEntityDescription(
        key="battery",
        translation_key="battery",
        device_class=SensorDeviceClass.BATTERY,
        native_unit_of_measurement=PERCENTAGE,
        state_class=SensorStateClass.MEASUREMENT,
        value_fn=lambda coordinator: (
            state.battery if (state := coordinator.get_device_state()) else None
        ),
    ),
    NavimowSensorEntityDescription(
        key="zone",
        name="Zone",
        icon="mdi:map-marker",
        value_fn=lambda c: target_zone(
            c.get_device_location(),
            state.state if (state := c.get_device_state()) else None,
        ),
    ),
    NavimowSensorEntityDescription(
        key="position_x",
        name="Position X",
        native_unit_of_measurement="m",
        value_fn=lambda c: (loc.get("x") if (loc := c.get_device_location()) else None),
    ),
    NavimowSensorEntityDescription(
        key="position_y",
        name="Position Y",
        native_unit_of_measurement="m",
        value_fn=lambda c: (loc.get("y") if (loc := c.get_device_location()) else None),
    ),
    NavimowSensorEntityDescription(
        key="heading",
        name="Heading",
        native_unit_of_measurement="°",
        icon="mdi:compass",
        value_fn=lambda c: (
            round(math.degrees(loc["theta"]) % 360, 1)
            if (loc := c.get_device_location()) and loc.get("theta") is not None
            else None
        ),
    ),
    NavimowSensorEntityDescription(
        key="mowing_zone",
        name="Mowing zone",
        icon="mdi:robot-mower",
        value_fn=lambda c: (
            loc.get("mow_boundary") if (loc := c.get_device_location()) else None
        ),
    ),
    NavimowSensorEntityDescription(
        key="dock_x",
        name="Dock X",
        native_unit_of_measurement="m",
        icon="mdi:home-map-marker",
        value_fn=lambda c: (
            round(d["x"], 2) if (d := c.get_dock_position()) and d.get("n") else None
        ),
    ),
    NavimowSensorEntityDescription(
        key="dock_y",
        name="Dock Y",
        native_unit_of_measurement="m",
        icon="mdi:home-map-marker",
        value_fn=lambda c: (
            round(d["y"], 2) if (d := c.get_dock_position()) and d.get("n") else None
        ),
    ),
    NavimowSensorEntityDescription(
        key="mow_progress",
        name="Mow progress",
        icon="mdi:progress-check",
        native_unit_of_measurement=PERCENTAGE,
        state_class=SensorStateClass.MEASUREMENT,
        value_fn=lambda c: progress_percent(c.get_device_location())[0],
    ),
    NavimowSensorEntityDescription(
        key="rest_status",
        name="REST status",
        icon="mdi:cloud-sync-outline",
        entity_category=EntityCategory.DIAGNOSTIC,
        value_fn=lambda c: c.get_rest_details()[0],
    ),
    NavimowSensorEntityDescription(
        key="rejected_input",
        name="Rejected input",
        icon="mdi:message-alert-outline",
        entity_category=EntityCategory.DIAGNOSTIC,
        value_fn=lambda c: c.get_rejected()[0],
    ),
    NavimowSensorEntityDescription(
        key="data_source",
        name="Data source",
        icon="mdi:database-sync",
        entity_category=EntityCategory.DIAGNOSTIC,
        value_fn=lambda c: c.get_data_source(),
    ),
)


async def async_setup_entry(
    hass: HomeAssistant,
    config_entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Set up Navimow sensors from a config entry."""
    data = hass.data[DOMAIN][config_entry.entry_id]
    devices = data["devices"]
    coordinators: dict[str, NavimowCoordinator] = data["coordinators"]

    entities: list[NavimowSensor] = []
    for device in devices:
        coordinator = coordinators[device.id]
        for description in SENSOR_DESCRIPTIONS:
            if description.key in ("dock_x", "dock_y"):
                cls = NavimowDockSensor
            elif description.key in RESTORING_KEYS:
                cls = NavimowLocationSensor
            else:
                cls = NavimowSensor
            entities.append(
                cls(
                    coordinator=coordinator,
                    entity_description=description,
                )
            )
    health = data["health"]
    entities.extend(NavimowLastMessageSensor(health, device) for device in devices)
    entities.extend(NavimowCollectorStatusSensor(health, device) for device in devices)
    async_add_entities(entities)


# Location sensors that restore their last recorded state on startup.
RESTORING_KEYS = ("position_x", "mowing_zone", "mow_progress", "zone")


class NavimowSensor(CoordinatorEntity[NavimowCoordinator], SensorEntity):
    """Representation of a Navimow sensor."""

    entity_description: NavimowSensorEntityDescription
    _attr_has_entity_name = True

    def __init__(
        self,
        coordinator: NavimowCoordinator,
        entity_description: NavimowSensorEntityDescription,
    ) -> None:
        super().__init__(coordinator)
        self.entity_description = entity_description

        device = coordinator.device
        self._attr_unique_id = f"{DOMAIN}_{device.id}_{entity_description.key}"
        self._attr_device_info = DeviceInfo(
            identifiers={(DOMAIN, device.id)},
            name=device.name,
            manufacturer="Navimow",
            model=device.model or "Unknown",
            sw_version=device.firmware_version or None,
            serial_number=device.serial_number or device.id,
        )

    @property
    def available(self) -> bool:
        if self.coordinator.get_device_state() is not None:
            return True
        return super().available

    @property
    def native_value(self) -> Any:
        """Return sensor value from coordinator."""
        return self.entity_description.value_fn(self.coordinator)

    @property
    def extra_state_attributes(self) -> dict[str, Any] | None:
        """Attributes grouped by the source or message type that produces them."""
        key = self.entity_description.key
        if key == "data_source":
            return self.coordinator.get_source_details()
        if key == "rest_status":
            return self.coordinator.get_rest_details()[1]
        if key == "rejected_input":
            return self.coordinator.get_rejected()[1]
        loc = self.coordinator.get_device_location()
        if not loc:
            return None
        restored = lambda *groups: any(loc.get(f"{g}_restored") for g in groups)
        if key == "zone":
            # type-3 target and type-4 delay
            return {
                "partition_ids": loc.get("partition_ids"),
                "target_time_ms": loc.get("target_time_ms"),
                "target_last_time_ms": loc.get("target_last_time_ms"),
                "task_delay": loc.get("task_delay"),
                "delay_received_at": loc.get("delay_received_at"),
                "is_restored": restored("target", "delay"),
            }
        if key == "position_x":
            # the complete latest type-1 pose, as one observation
            if loc.get("x") is None:
                return None
            return {
                "y": loc.get("y"),
                "theta_rad": loc.get("theta"),
                "vehicle_state": loc.get("vehicle_state"),
                "pose_time_ms": loc.get("pose_time"),
                "received_at": loc.get("received_at"),
                "source": POSE_SOURCE,
                "is_restored": restored("pose"),
            }
        if key == "mowing_zone":
            # the latest type-2 task entry, as one observation
            task = loc.get("task")
            if not task:
                return None
            return {**task, "is_restored": restored("task")}
        if key == "mow_progress":
            source = progress_percent(loc)[1]
            return {
                "progress_source": source,
                "is_restored": (
                    restored("progress") if source == "route"
                    else restored("task") if source == "percentage"
                    else False
                ),
            }
        return None


class NavimowLocationSensor(NavimowSensor, RestoreEntity):
    """Location sensor that seeds the coordinator with its last recorded state.

    The location cache lives in memory, so after a restart every location
    sensor would read unknown until its message type arrives again: up to
    five minutes for a docked pose, and not before the next mow for the task
    report. Restoring the last state fills that gap; the ``is_restored``
    attribute stays true until live data of the same type replaces it.
    (Not ``restored``: Home Assistant reserves that attribute name for
    entity-registry placeholders and the recorder strips it from history.)
    """

    async def async_added_to_hass(self) -> None:
        await super().async_added_to_hass()
        last = await self.async_get_last_state()
        if last is None:
            return
        for group, fields in restore_location_groups(
            self.entity_description.key, last.state, dict(last.attributes)
        ):
            self.coordinator.restore_location(group, fields)


class NavimowDockSensor(NavimowSensor, RestoreSensor):
    """Dock position sensor that survives HA restarts.

    The dock estimate is learned in-memory by the coordinator while the mower
    is docked/charging. After a restart, the previously learned value is
    restored from HA's state storage and shown until live samples replace it.
    """

    _restored_value: float | None = None

    async def async_added_to_hass(self) -> None:
        await super().async_added_to_hass()
        if (data := await self.async_get_last_sensor_data()) is not None:
            try:
                self._restored_value = float(data.native_value)
            except (TypeError, ValueError):
                self._restored_value = None

    @property
    def native_value(self) -> Any:
        live = self.entity_description.value_fn(self.coordinator)
        return live if live is not None else self._restored_value

    @property
    def extra_state_attributes(self) -> dict[str, Any] | None:
        d = self.coordinator.get_dock_position()
        return {
            "samples": (d or {}).get("n", 0),
            "source": "live" if d and d.get("n") else (
                "restored" if self._restored_value is not None else "none"
            ),
        }


class NavimowLastMessageSensor(SensorEntity):
    """When the last MQTT message for this mower arrived (health.py).

    A mower that is out sends a pose every two seconds, so the state is
    written at most once per LAST_MESSAGE_RESOLUTION seconds; a message
    held back by that limit is written when the interval ends, so the value
    is never more than one interval behind.
    """

    _attr_has_entity_name = True
    _attr_name = "Last message"
    _attr_device_class = SensorDeviceClass.TIMESTAMP
    _attr_entity_category = EntityCategory.DIAGNOSTIC
    _attr_icon = "mdi:message-arrow-left-outline"
    _attr_should_poll = False
    _clock = staticmethod(time.monotonic)

    def __init__(self, health: CollectorHealth, device: Any) -> None:
        self._health = health
        self._device_id = device.id
        self._attr_unique_id = f"{DOMAIN}_{device.id}_last_message"
        self._attr_device_info = device_info(device)
        self._shown: datetime | None = None
        self._written_at: float | None = None
        self._cancel_flush: Callable[[], None] | None = None

    async def async_added_to_hass(self) -> None:
        self.async_on_remove(self._health.async_add_message_listener(self._on_message))
        self.async_on_remove(self._cancel_pending_flush)

    @property
    def native_value(self) -> datetime | None:
        return self._shown

    @callback
    def _on_message(self, device_id: str) -> None:
        if device_id != self._device_id or self._cancel_flush is not None:
            return  # another mower, or a write is already scheduled
        now = self._clock()
        if self._written_at is None or now - self._written_at >= LAST_MESSAGE_RESOLUTION:
            self._write()
        else:
            self._cancel_flush = async_call_later(
                self.hass, LAST_MESSAGE_RESOLUTION - (now - self._written_at), self._flush
            )

    @callback
    def _flush(self, _now: Any) -> None:
        self._cancel_flush = None
        self._write()

    @callback
    def _write(self) -> None:
        self._shown = self._health.last_message_at.get(self._device_id)
        self._written_at = self._clock()
        self.async_write_ha_state()

    @callback
    def _cancel_pending_flush(self) -> None:
        if self._cancel_flush is not None:
            self._cancel_flush()
            self._cancel_flush = None


class NavimowCollectorStatusSensor(SensorEntity):
    """The cloud session's health in one place (health.py): a short status
    and the counters and latest errors behind it. The session belongs to the
    config entry, so every mower on the entry shows the same values."""

    _attr_has_entity_name = True
    _attr_name = "Collector status"
    _attr_entity_category = EntityCategory.DIAGNOSTIC
    _attr_icon = "mdi:cloud-check-outline"
    _attr_should_poll = False

    def __init__(self, health: CollectorHealth, device: Any) -> None:
        self._health = health
        self._attr_unique_id = f"{DOMAIN}_{device.id}_collector_status"
        self._attr_device_info = device_info(device)

    async def async_added_to_hass(self) -> None:
        self.async_on_remove(self._health.async_add_listener(self.async_write_ha_state))

    @property
    def native_value(self) -> str:
        return self._health.status

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        return self._health.status_attributes()
