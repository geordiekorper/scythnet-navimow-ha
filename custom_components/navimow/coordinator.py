"""Per-mower coordinator, push-fed by the SDK's client.

NavimowClient merges the MQTT state messages and the REST status polls into
one MowerState per mower and delivers each new one through on_state; the
coordinator keeps the latest, the last one of each source, the location
record it carries, and what the location sensors restored after a restart,
and tells the entities. It asks for nothing itself: there is no update
interval and no fetch of its own.
"""
from __future__ import annotations

import logging
from datetime import datetime
from typing import Any

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator

from mower_sdk.models import (
    Device,
    DeviceAttributesMessage,
    DeviceEventMessage,
    DeviceLocation,
    DeviceStatus,
    REST_STATUS_KNOWN_FIELDS,
    RejectedMessage,
)
from mower_sdk.navimow_client import MowerState, NavimowClient, StateSource

from .const import DOMAIN
from .location import TASK_ATTRIBUTES
from .rejected import rejection_record

_LOGGER = logging.getLogger(__name__)

# The data_source sensor's labels for the client's sources. The labels are
# the official integration's; its third one, mqtt_cache (a message adopted
# from the SDK's cache because it arrived before the callback existed),
# cannot occur with the client, which registers its handler at build.
DATA_SOURCE_LABELS = {StateSource.MQTT: "mqtt_push", StateSource.REST: "http_fallback"}

# The record fields each restored group stands for. A group stays restored
# until a live record differs from the restored one in these fields: the SDK
# refuses an entry older than the restored marks, so an entry it applies
# moves at least one of them.
_TASK_FIELDS = tuple(name for name in TASK_ATTRIBUTES.values() if name != "route_progress")
_GROUP_FIELDS: dict[str, tuple[str, ...]] = {
    "pose": ("pose_at", "pose_received_at", "x", "y", "theta", "vehicle_state"),
    "task": _TASK_FIELDS,
    "progress": ("route_progress",),
    "target": ("target_at", "target_last_at", "partition_ids"),
    "delay": ("delay_received_at", "task_delay"),
}


def _group_signature(group: str, location: DeviceLocation) -> tuple[Any, ...]:
    fields = tuple(getattr(location, name) for name in _GROUP_FIELDS[group])
    if group == "progress":
        # An entry that carries currentMowProgress, an explicit null included,
        # changes the reading or its source.
        fields += (location.progress_source,)
    return fields


def _has_task_fields(location: DeviceLocation) -> bool:
    return location.marks.get(2) is not None or any(
        getattr(location, name) is not None for name in TASK_ATTRIBUTES.values()
    )


class NavimowCoordinator(DataUpdateCoordinator[dict[str, Any]]):
    """Coordinator for one mower's data, fed by the client."""

    def __init__(
        self,
        hass: HomeAssistant,
        client: NavimowClient,
        device: Device,
        config_entry: ConfigEntry | None = None,
    ) -> None:
        super().__init__(
            hass,
            _LOGGER,
            config_entry=config_entry,
            name=DOMAIN,
            update_interval=None,
        )
        self.client = client
        self.device = device
        self.data: dict[str, Any] = {}
        # The latest state, and the last one of each source: the data_source
        # sensor shows the two side by side.
        self._state: MowerState | None = None
        self._mqtt_state: MowerState | None = None
        self._rest_state: MowerState | None = None
        # The latest REST reply, every poll (polls.py), shown or not: the
        # rest_status sensor writes a row per poll.
        self._rest_status: DeviceStatus | None = None
        self._rest_polled_at: str | None = None
        self._last_attributes: DeviceAttributesMessage | None = None
        # The location record as of the last state, or as restored.
        self._last_location: DeviceLocation | None = None
        # The location sensors' restored fields (DeviceLocation names), the
        # record assembled from them, and the groups still restored rather
        # than live.
        self._restored_fields: dict[str, Any] = {}
        self._restored_location: DeviceLocation | None = None
        self._restored_groups: set[str] = set()
        # Whether a task entry was applied or a task group restored: the
        # record's task fields may all be None after a real task report.
        self._has_task = False
        # When the delay flag took its current value: the receipt time of the
        # first delay entry that reported it. The record's delay_received_at
        # is the latest entry's, and the mower repeats the report about every
        # two minutes, so shown as an attribute it would write a recorder row
        # on every repeat.
        self._delay_since: datetime | None = None
        # Input received but not applied (rejected.py): a running count since
        # start-up and the latest item.
        self._rejected_count = 0
        self._last_rejected: dict[str, Any] | None = None

    @callback
    def async_register(self) -> None:
        """Register with the client, before it connects, so nothing is missed."""
        device_id = self.device.id
        self.client.on_state(self.ingest_state, device_id=device_id)
        self.client.on_event(self._handle_event, device_id=device_id)
        self.client.on_attributes(self._handle_attributes, device_id=device_id)
        self.client.on_rejected(self._handle_rejected, device_id=device_id)

    async def _async_update_data(self) -> dict[str, Any]:
        # Push-fed: nothing asks for a refresh, and one would change nothing.
        return self.data

    def _build_data(self) -> dict[str, Any]:
        return {
            "device": self.device,
            "state": self._state,
            "attributes": self._last_attributes,
            "location": self._last_location,
            "meta": {"last_data_source": self.get_data_source()},
        }

    # ---- what the client delivers -----------------------------------------

    @callback
    def ingest_state(self, state: MowerState) -> None:
        """A new state of this mower (client.on_state), on the event loop."""
        self._state = state
        if state.source is StateSource.MQTT:
            self._mqtt_state = state
        else:
            self._rest_state = state
        if state.location is not None:
            self.apply_location(state.location)
        self.async_set_updated_data(self._build_data())

    @callback
    def apply_location(self, location: DeviceLocation) -> None:
        """A new location record: end the restore of each group it replaces,
        and keep the time the delay flag was first reported with its value."""
        previous = self._last_location
        if self._restored_groups and self._restored_location is not None:
            for group in list(self._restored_groups):
                if _group_signature(group, location) != _group_signature(
                    group, self._restored_location
                ):
                    self._restored_groups.discard(group)
        if previous is None or (location.delay_received_at, location.task_delay) != (
            previous.delay_received_at, previous.task_delay
        ):
            # A delay report: a repeat of the current flag keeps the time it
            # was first seen, a restored one included.
            if (
                previous is None
                or self._delay_since is None
                or previous.task_delay != location.task_delay
            ):
                self._delay_since = location.delay_received_at
        if _has_task_fields(location):
            self._has_task = True
        self._last_location = location

    @callback
    def note_rest_status(self, status: DeviceStatus, polled_at: datetime) -> None:
        """One reply of the client's status poll (polls.py), shown or not."""
        self._rest_status = status
        self._rest_polled_at = polled_at.isoformat()
        self.async_set_updated_data(self._build_data())

    @callback
    def _handle_event(self, event: DeviceEventMessage) -> None:
        # Nothing here uses the event channel: kept whole as rejected input,
        # so its content can be studied once a mower sends any.
        self.record_rejected("event", None, "unknown_channel", event.raw)

    @callback
    def _handle_attributes(self, attrs: DeviceAttributesMessage) -> None:
        _LOGGER.debug("MQTT attributes received: device=%s", attrs.device_id)
        # Attributes surface only as an opaque attribute of the lawn mower;
        # kept whole as rejected input as well.
        self.record_rejected("attributes", None, "unknown_channel", attrs.raw)
        self._last_attributes = attrs
        self.async_set_updated_data(self._build_data())

    @callback
    def _handle_rejected(self, message: RejectedMessage) -> None:
        # Any channel: a late, implausibly stamped or unparsable state
        # message, a state field nobody knows, a malformed event or
        # attributes payload, a location entry the decoder refused. Recorded
        # with the bytes as the mower sent them, once per message.
        payload = message.original if message.original is not None else message.payload
        self.record_rejected(
            message.channel, message.topic, message.reason,
            payload.decode("utf-8", "replace"), list(message.reasons),
        )

    @callback
    def record_rejected(
        self,
        channel: str,
        topic: str | None,
        reason: str,
        payload: Any,
        reasons: list[str] | None = None,
    ) -> None:
        """Record input that was received but not applied (see rejected.py).

        Each call is one state update of the rejected_input sensor, so the
        recorder keeps every item.
        """
        from homeassistant.util import dt as dt_util

        self._last_rejected = rejection_record(
            channel, topic, reason, payload, dt_util.utcnow().isoformat(), reasons
        )
        self._rejected_count += 1
        self.async_set_updated_data(self._build_data())

    # ---- the restore after a restart -----------------------------------------

    @callback
    def restore_location(self, group: str, fields: dict[str, Any]) -> None:
        """Collect a location sensor's restored fields (DeviceLocation names).

        The entities show the record assembled so far at once; the whole of
        it goes to the client in async_finish_restore(). A group stays
        restored until a live record differs from it in the group's fields.
        Live data wins: nothing is restored once a live record exists.
        """
        if not fields or (self._last_location is not None and not self._restored_groups):
            return
        self._restored_fields.update(fields)
        self._restored_groups.add(group)
        if group == "task":
            self._has_task = True
        self._last_location = DeviceLocation.from_dict(
            {**self._restored_fields, "device_id": self.device.id}
        )
        self._restored_location = self._last_location
        if group == "delay":
            # The zone sensor recorded the time its flag was first seen.
            self._delay_since = self._last_location.delay_received_at
        self.async_set_updated_data(self._build_data())

    @callback
    def async_finish_restore(self) -> None:
        """Hand the restored record to the client, before it connects.

        The client queues it and installs it when it builds the SDK's
        decoder, whose high-water marks it seeds, so a late entry older than
        what was shown before the restart is rejected as stale.
        """
        if self._restored_groups and self._last_location is not None:
            self.client.restore_location(self.device.id, self._last_location)

    def has_task_report(self) -> bool:
        """Whether a task entry was applied, or a task group restored."""
        return self._has_task

    def get_delay_since(self) -> datetime | None:
        """When the delay flag was first reported with its current value (UTC)."""
        return self._delay_since

    def is_group_restored(self, group: str) -> bool:
        """Whether a location group still shows its restored value."""
        return group in self._restored_groups

    # ---- what the entities read ----------------------------------------------

    def get_device_state(self) -> MowerState | None:
        return self._state

    def get_device_location(self) -> DeviceLocation | None:
        return self._last_location

    def get_device_attributes(self) -> DeviceAttributesMessage | None:
        return self._last_attributes

    def get_device_info(self) -> Any | None:
        return self.device

    def get_dock_position(self) -> dict | None:
        """The dock the SDK learned from docked poses {"x","y","n"}, or None."""
        loc = self._last_location
        if loc is None or loc.dock_x is None or loc.dock_y is None:
            return None
        return {"x": loc.dock_x, "y": loc.dock_y, "n": loc.dock_samples}

    def get_data_source(self) -> str:
        """Source of the shown state: mqtt_push, http_fallback or none."""
        if self._state is None:
            return "none"
        return DATA_SOURCE_LABELS.get(self._state.source, self._state.source.value)

    def get_source_details(self) -> dict[str, Any]:
        """The last MQTT state message and the last REST poll, side by side.

        Device timestamps (``mqtt_timestamp``, ``rest_timestamp``) are what
        the source supplied and stay None when it supplied nothing; HA's
        own clock is only ever in ``mqtt_received_at`` / ``rest_polled_at``.
        """
        mqtt = self._mqtt_state
        message = mqtt.message if mqtt is not None else None
        rest = self._rest_status
        extra = (rest.extra if rest else None) or {}
        return {
            "mqtt_state": message.state if message else None,
            "mqtt_raw_state": (message.metrics or {}).get("raw_state") if message else None,
            "mqtt_battery": message.battery if message else None,
            "mqtt_timestamp": message.timestamp if message else None,
            "mqtt_received_at": mqtt.received_at.isoformat() if mqtt else None,
            "rest_status": rest.status.value if rest else None,
            "rest_vehicle_state": extra.get("vehicleState"),
            "rest_battery": rest.battery if rest else None,
            "rest_battery_level": extra.get("descriptiveCapacityRemaining"),
            "rest_timestamp": rest.timestamp if rest else None,
            "rest_polled_at": self._rest_polled_at,
        }

    def get_rest_details(self) -> tuple[str | None, dict[str, Any] | None]:
        """The latest REST reply: its raw vehicleState, and the rest.

        Fields outside the SDK's REST_STATUS_KNOWN_FIELDS are kept under
        ``unknown_fields`` so a field the cloud starts sending is visible in
        history.
        """
        status = self._rest_status
        if status is None:
            return None, None
        extra = status.extra or {}
        unknown = {k: v for k, v in extra.items() if k not in REST_STATUS_KNOWN_FIELDS}
        vehicle_state = extra.get("vehicleState")
        return (
            vehicle_state if isinstance(vehicle_state, str) else None,
            {
                "battery": status.battery,
                "battery_level": extra.get("descriptiveCapacityRemaining"),
                "polled_at": self._rest_polled_at,
                "unknown_fields": unknown or None,
            },
        )

    def get_rejected(self) -> tuple[int, dict[str, Any] | None]:
        """Count of rejected items since start-up, and the latest one."""
        return self._rejected_count, self._last_rejected
