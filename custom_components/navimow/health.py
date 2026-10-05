"""Collector health: the state of the cloud connection, for diagnostics.

Without it, a quiet mower and a collector that has stopped receiving look
the same in history. One CollectorHealth per config entry follows the MQTT
connection, and the diagnostic entities of every mower on the entry show
it, so the recorder keeps the connection's history beside the mower's.

The SDK's MQTT client counts connects, disconnects, failed connects and
rebuilds, keeps the reason for each, and times every message per device and
channel; CollectorHealth reads those from the client. What it keeps itself
is what the SDK has no equivalent for: the connected flag as the client's
hooks report it (note_connected and the others, called from those hooks),
wall-clock stamps of the events, the credential refreshes, the token expiry,
the REST poll's outcome, and the listeners.
"""
from __future__ import annotations

import time
from collections.abc import Callable
from datetime import datetime
from typing import Any

from homeassistant.core import CALLBACK_TYPE, callback
from homeassistant.util import dt as dt_util

class CollectorHealth:
    """Connection state of one config entry's cloud session."""

    def __init__(self, mqtt: Any) -> None:
        # The SDK's NavimowMQTT: its counters, reasons, client id and message times.
        self.mqtt = mqtt
        self.connected = False
        self.connected_at: str | None = None
        self.disconnected_at: str | None = None
        self.connect_failed_at: str | None = None
        # When the client last connected (monotonic, for the watchdog).
        self.connected_monotonic: float | None = None
        self.credential_refreshes = 0
        self.last_rebuild_at: str | None = None
        self._rebuilds_stamped = 0
        self.token_expires_at: str | None = None
        # The entry's REST poller (rest_poll.py), for its latest outcome.
        self.poller: Any = None
        self._poll_outcome: tuple[str | None, str | None] = (None, None)
        self._listeners: list[Callable[[], None]] = []
        self._message_listeners: list[Callable[[str], None]] = []

    # Read from the SDK's client, which counts every client it builds.
    @property
    def connects(self) -> int:
        return self.mqtt.connects

    @property
    def disconnects(self) -> int:
        return self.mqtt.disconnects

    @property
    def connect_failures(self) -> int:
        return self.mqtt.connect_failures

    @property
    def rebuilds(self) -> int:
        return self.mqtt.rebuilds

    @property
    def last_rebuild_reason(self) -> str | None:
        return self.mqtt.last_rebuild_reason

    @property
    def client_id(self) -> str | None:
        return self.mqtt.client_id

    @property
    def disconnect_reason(self) -> str | None:
        return self.mqtt.last_disconnect_reason

    @property
    def connect_fail_reason(self) -> str | None:
        return self.mqtt.last_connect_fail_reason

    def last_message_at(self, device_id: str) -> datetime | None:
        """When the last MQTT message for the device arrived, on any channel."""
        return self.mqtt.last_message_at(device_id)

    def location_age(self, device_id: str) -> float | None:
        """Seconds since the device's last location message, or None if none has come."""
        return self.mqtt.last_message_age(device_id, "location")

    @callback
    def async_add_listener(self, update: Callable[[], None]) -> CALLBACK_TYPE:
        """Call ``update`` after every change; returns the remover."""
        self._listeners.append(update)

        def remove() -> None:
            if update in self._listeners:
                self._listeners.remove(update)

        return remove

    @callback
    def async_add_message_listener(self, update: Callable[[str], None]) -> CALLBACK_TYPE:
        """Call ``update(device_id)`` for every message received; kept apart
        from the other listeners because it fires every two seconds while a
        mower is out."""
        self._message_listeners.append(update)

        def remove() -> None:
            if update in self._message_listeners:
                self._message_listeners.remove(update)

        return remove

    @callback
    def note_message(self, device_id: str) -> None:
        """A message for the device arrived: tell the message listeners."""
        for update in list(self._message_listeners):
            update(device_id)

    @callback
    def _notify(self) -> None:
        for update in list(self._listeners):
            update()

    # Called from the client's hooks, which run after the client has counted
    # the event and recorded its reason.
    @callback
    def note_connected(self) -> None:
        self.connected = True
        self.connected_monotonic = time.monotonic()
        self.connected_at = dt_util.utcnow().isoformat()
        self._notify()

    @callback
    def note_disconnected(self) -> None:
        self.connected = False
        self.disconnected_at = dt_util.utcnow().isoformat()
        self._notify()

    @callback
    def note_connect_failed(self) -> None:
        self.connected = False
        self.connect_failed_at = dt_util.utcnow().isoformat()
        self._notify()

    @callback
    def note_credential_refresh(self) -> None:
        """Fresh broker credentials were fetched after a disconnect."""
        self.credential_refreshes += 1
        self._notify()

    @callback
    def note_rebuild(self) -> None:
        """Stamp last_rebuild_at if the client was rebuilt since the last call.

        Called after each call that may rebuild the client (a watchdog
        rebuild, a credential update while disconnected), so the stamp goes
        with the count and the reason the client keeps, whichever path
        rebuilt it.
        """
        if self.mqtt.rebuilds != self._rebuilds_stamped:
            self._rebuilds_stamped = self.mqtt.rebuilds
            self.last_rebuild_at = dt_util.utcnow().isoformat()
            self._notify()

    @callback
    def note_token(self, expires_at: Any) -> None:
        """The OAuth token in use and when it expires (epoch seconds)."""
        try:
            stamp = dt_util.utc_from_timestamp(float(expires_at)).isoformat()
        except (TypeError, ValueError, OverflowError, OSError):
            return
        if stamp != self.token_expires_at:
            self.token_expires_at = stamp
            self._notify()

    @callback
    def note_settings_changed(self) -> None:
        """A setting shown in the status (the poll interval) changed."""
        self._notify()

    @callback
    def note_poll(self) -> None:
        """A REST poll finished; tell the listeners only if its error changed,
        so a healthy poll does not rewrite the status every two minutes."""
        poller = self.poller
        outcome = (poller.last_error, poller.last_error_at) if poller else (None, None)
        if outcome != self._poll_outcome:
            self._poll_outcome = outcome
            self._notify()

    @property
    def status(self) -> str:
        """ok, starting (never connected yet), disconnected, or poll_failing."""
        if not self.connected:
            return "disconnected" if self.connects or self.connect_failures else "starting"
        if self.poller is not None and self.poller.last_error:
            return "poll_failing"
        return "ok"

    def status_attributes(self) -> dict[str, Any]:
        poller = self.poller
        return {
            "connects": self.connects,
            "disconnects": self.disconnects,
            "connect_failures": self.connect_failures,
            "credential_refreshes": self.credential_refreshes,
            "rebuilds": self.rebuilds,
            "last_rebuild_reason": self.last_rebuild_reason,
            "last_rebuild_at": self.last_rebuild_at,
            "poll_interval": poller.interval if poller else None,
            "last_poll_error": poller.last_error if poller else None,
            "last_poll_error_at": poller.last_error_at if poller else None,
            "token_expires_at": self.token_expires_at,
        }

    def connection_attributes(self) -> dict[str, Any]:
        return {
            "client_id": self.client_id,
            "connected_at": self.connected_at,
            "disconnected_at": self.disconnected_at,
            "disconnect_reason": self.disconnect_reason,
            "connect_failed_at": self.connect_failed_at,
            "connect_fail_reason": self.connect_fail_reason,
        }


def device_id_from_topic(topic: str) -> str | None:
    """The device id of a /downlink/vehicle/<id>/realtimeDate/<channel> topic, else None.

    The same topics the SDK's client times messages for, so a listener told
    about a device finds its time in last_message_at.
    """
    parts = topic.split("/")
    if parts and parts[0] == "":
        parts = parts[1:]
    if (
        len(parts) != 5
        or parts[0] != "downlink"
        or parts[1] != "vehicle"
        or parts[3] != "realtimeDate"
        or not parts[2]
    ):
        return None
    return parts[2]
