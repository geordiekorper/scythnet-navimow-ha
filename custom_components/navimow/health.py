"""Collector health: the state of the cloud connection, for diagnostics.

Without it, a quiet mower and a collector that has stopped receiving look
the same in history. One CollectorHealth per config entry follows the MQTT
connection, and the diagnostic entities of every mower on the entry show
it, so the recorder keeps the connection's history beside the mower's.

The SDK reports neither why a connection dropped nor that a connect failed
(it logs a refused CONNACK and returns, and never sets paho's
on_connect_fail, which is how a token rejected at the WebSocket upgrade
shows). instrument_mqtt() therefore wraps the paho callbacks of the SDK's
client, including every client the SDK builds later.
"""
from __future__ import annotations

import asyncio
from collections.abc import Callable
from datetime import datetime
from typing import Any

from homeassistant.core import CALLBACK_TYPE, callback
from homeassistant.util import dt as dt_util


class CollectorHealth:
    """Connection state of one config entry's cloud session."""

    def __init__(self) -> None:
        self.connected = False
        self.client_id: str | None = None
        self.connected_at: str | None = None
        self.disconnected_at: str | None = None
        self.disconnect_reason: str | None = None
        self.connect_failed_at: str | None = None
        self.connect_fail_reason: str | None = None
        # When the last MQTT message for each device arrived.
        self.last_message_at: dict[str, datetime] = {}
        self._listeners: list[Callable[[], None]] = []
        self._message_listeners: list[Callable[[str], None]] = []

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
        self.last_message_at[device_id] = dt_util.utcnow()
        for update in list(self._message_listeners):
            update(device_id)

    @callback
    def _notify(self) -> None:
        for update in list(self._listeners):
            update()

    @callback
    def note_connected(self, client_id: str | None = None) -> None:
        self.connected = True
        self.client_id = client_id or self.client_id
        self.connected_at = dt_util.utcnow().isoformat()
        self._notify()

    @callback
    def note_disconnected(self, reason: str | None = None) -> None:
        self.connected = False
        self.disconnected_at = dt_util.utcnow().isoformat()
        self.disconnect_reason = reason
        self._notify()

    @callback
    def note_connect_failed(self, reason: str) -> None:
        self.connected = False
        self.connect_failed_at = dt_util.utcnow().isoformat()
        self.connect_fail_reason = reason
        self._notify()

    def connection_attributes(self) -> dict[str, Any]:
        return {
            "client_id": self.client_id,
            "connected_at": self.connected_at,
            "disconnected_at": self.disconnected_at,
            "disconnect_reason": self.disconnect_reason,
            "connect_failed_at": self.connect_failed_at,
            "connect_fail_reason": self.connect_fail_reason,
        }


def _client_id(client: Any) -> str | None:
    raw = getattr(client, "_client_id", None)
    if isinstance(raw, (bytes, bytearray)):
        return raw.decode("utf-8", errors="replace") or None
    return str(raw) if raw else None


# paho's CONNACK return codes (MQTT 3.1.1), for readable failure reasons.
CONNACK_CODES = {
    1: "unacceptable protocol version",
    2: "identifier rejected",
    3: "server unavailable",
    4: "bad user name or password",
    5: "not authorised",
}


def instrument_mqtt(mqtt: Any, loop: asyncio.AbstractEventLoop, health: CollectorHealth) -> None:
    """Report the SDK client's connects, disconnects and failed connects to
    ``health``. ``mqtt`` is the SDK's NavimowMQTT; paho calls back on its own
    thread, so every report is handed to ``loop``.
    """
    original_connect = mqtt._on_connect
    original_disconnect = mqtt._on_disconnect
    original_build = mqtt._build_new_client

    def report(func: Callable[..., None], *args: Any) -> None:
        if loop.is_running():
            loop.call_soon_threadsafe(func, *args)

    def on_connect(client: Any, userdata: Any, flags: Any, rc: Any) -> None:
        if rc == 0:
            report(health.note_connected, _client_id(client))
        else:
            report(health.note_connect_failed, f"refused: {CONNACK_CODES.get(rc, f'rc={rc}')}")
        original_connect(client, userdata, flags, rc)

    def on_disconnect(client: Any, userdata: Any, rc: Any) -> None:
        reason = "requested" if rc == 0 else f"lost (rc={rc})"
        report(health.note_disconnected, reason)
        original_disconnect(client, userdata, rc)

    def on_connect_fail(_client: Any, _userdata: Any) -> None:
        # No CONNACK at all: network failure, or the bearer token rejected
        # at the WebSocket upgrade.
        report(health.note_connect_failed, "connection failed before CONNACK")

    def attach(client: Any) -> Any:
        client.on_connect = on_connect
        client.on_disconnect = on_disconnect
        client.on_connect_fail = on_connect_fail
        return client

    # _build_new_client wires self._on_connect / self._on_disconnect into
    # each client it builds; the instance attributes make that the wrappers,
    # and the wrapped builder adds on_connect_fail.
    mqtt._on_connect = on_connect
    mqtt._on_disconnect = on_disconnect
    mqtt._build_new_client = lambda: attach(original_build())
    attach(mqtt.client)
    if mqtt.is_connected:  # connected before the wrappers were in place
        health.note_connected(_client_id(mqtt.client))
