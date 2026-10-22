"""Collector health: the state of the cloud connection, for diagnostics.

Without it, a quiet mower and a collector that has stopped receiving look
the same in history. One CollectorHealth per config entry follows the SDK
client's connection, and the diagnostic entities of every mower on the entry
show it, so the recorder keeps the connection's history beside the mower's.

The SDK's MQTT client counts connects, disconnects, failed connects and
rebuilds, keeps the reason and the time of each, and times every message per
device and channel; CollectorHealth reads those from it (client.mqtt, None
until the client has connected once). What it keeps itself is what the SDK
has no equivalent for: the connected flag as the client's connection events
report it, the recoveries from a refused connection, the rebuild's stamp, the
token expiry, the REST poll's outcome, and the listeners.
"""
from __future__ import annotations

from collections.abc import Callable
from datetime import datetime
from typing import Any

from homeassistant.core import CALLBACK_TYPE, callback
from homeassistant.util import dt as dt_util


def iso_or_none(stamp: datetime | None) -> str | None:
    """A UTC stamp as the entities show it, or None; shared with the sensors."""
    return None if stamp is None else stamp.isoformat()


class CollectorHealth:
    """Connection state of one config entry's cloud session."""

    def __init__(self, client: Any = None) -> None:
        # The SDK's NavimowClient; its mqtt is the paho wrapper with the
        # counters, reasons, client id and message times, None before the
        # first connect. Set after construction when the client needs the
        # health first (its token provider stamps the token expiry here).
        self.client = client
        self.connected = False
        # Recoveries: a connect that followed one or more failed attempts.
        # The client fetches the broker credentials again after a refused
        # connection, which is what the next successful connect shows.
        self.credential_refreshes = 0
        self._failed_since_connect = False
        self.last_rebuild_at: str | None = None
        self._rebuilds_stamped = 0
        self.token_expires_at: str | None = None
        # The REST poll: its interval (the entry's option, set by the setup)
        # and the latest failure, cleared by the next success.
        self.poll_interval: int | None = None
        self.last_poll_error: str | None = None
        self.last_poll_error_at: str | None = None
        self._listeners: list[Callable[[], None]] = []
        self._message_listeners: list[Callable[[str], None]] = []

    @property
    def mqtt(self) -> Any:
        """The SDK's MQTT client, or None before the first connect."""
        return None if self.client is None else self.client.mqtt

    # Read from the SDK's client, which counts every paho client it builds;
    # getattr on None gives the default before the first connect.
    @property
    def connects(self) -> int:
        return getattr(self.mqtt, "connects", 0)

    @property
    def disconnects(self) -> int:
        return getattr(self.mqtt, "disconnects", 0)

    @property
    def connect_failures(self) -> int:
        return getattr(self.mqtt, "connect_failures", 0)

    @property
    def rebuilds(self) -> int:
        return getattr(self.mqtt, "rebuilds", 0)

    @property
    def last_rebuild_reason(self) -> str | None:
        return getattr(self.mqtt, "last_rebuild_reason", None)

    @property
    def client_id(self) -> str | None:
        return getattr(self.mqtt, "client_id", None)

    @property
    def disconnect_reason(self) -> str | None:
        return getattr(self.mqtt, "last_disconnect_reason", None)

    @property
    def connect_fail_reason(self) -> str | None:
        return getattr(self.mqtt, "last_connect_fail_reason", None)

    # The stamps of the latest connect, disconnect and failed connect: set by
    # the client in the MQTT thread as each event happens, so they agree with
    # the reasons shown beside them.
    @property
    def connected_at(self) -> str | None:
        return iso_or_none(getattr(self.mqtt, "last_connected_at", None))

    @property
    def disconnected_at(self) -> str | None:
        return iso_or_none(getattr(self.mqtt, "last_disconnected_at", None))

    @property
    def connect_failed_at(self) -> str | None:
        return iso_or_none(getattr(self.mqtt, "last_connect_failed_at", None))

    def last_message_at(self, device_id: str) -> datetime | None:
        """When the last MQTT message for the device arrived, on any channel."""
        return mqtt.last_message_at(device_id) if (mqtt := self.mqtt) is not None else None

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

    @callback
    def note_connection(self, event: Any) -> None:
        """One of the client's ConnectionEvents: connected, disconnected or
        connect_failed. Registered with client.on_connection; the client has
        counted the event and recorded its reason by then, and a rebuilt
        client's connect shows up here too."""
        if event.kind == "connected":
            self.note_connected()
        elif event.kind == "disconnected":
            self.note_disconnected()
        else:
            self.note_connect_failed()
        self.note_rebuild()

    @callback
    def note_connected(self) -> None:
        self.connected = True
        if self._failed_since_connect:
            self._failed_since_connect = False
            self.credential_refreshes += 1
        self._notify()

    @callback
    def note_disconnected(self) -> None:
        self.connected = False
        self._notify()

    @callback
    def note_connect_failed(self) -> None:
        self.connected = False
        self._failed_since_connect = True
        self._notify()

    @callback
    def note_rebuild(self) -> None:
        """Stamp last_rebuild_at if the client was rebuilt since the last call.

        Called after every connection event, so the stamp goes with the count
        and the reason the client keeps, whichever path rebuilt it (the
        watchdog, a credential refresh while disconnected, a request).
        """
        if self.rebuilds != self._rebuilds_stamped:
            self._rebuilds_stamped = self.rebuilds
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
    def note_poll_ok(self) -> None:
        """A REST poll succeeded: a failure shown until now is over. Nothing
        is announced for a healthy poll after a healthy one, so the status
        is not rewritten at every poll."""
        if self.last_poll_error is not None:
            self.last_poll_error = None
            self.last_poll_error_at = None
            self._notify()

    @callback
    def note_poll_error(self, error: str) -> None:
        """A REST poll failed (the client's on_error, poll or command_poll);
        every failure is announced, so the history shows each one."""
        self.last_poll_error = error
        self.last_poll_error_at = dt_util.utcnow().isoformat()
        self._notify()

    @property
    def status(self) -> str:
        """ok, starting (never connected yet), disconnected, or poll_failing."""
        if not self.connected:
            return "disconnected" if self.connects or self.connect_failures else "starting"
        if self.last_poll_error is not None:
            return "poll_failing"
        return "ok"

    def status_attributes(self) -> dict[str, Any]:
        return {
            "connects": self.connects,
            "disconnects": self.disconnects,
            "connect_failures": self.connect_failures,
            "credential_refreshes": self.credential_refreshes,
            "rebuilds": self.rebuilds,
            "last_rebuild_reason": self.last_rebuild_reason,
            "last_rebuild_at": self.last_rebuild_at,
            "poll_interval": self.poll_interval,
            "last_poll_error": self.last_poll_error,
            "last_poll_error_at": self.last_poll_error_at,
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
