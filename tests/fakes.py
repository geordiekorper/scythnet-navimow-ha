"""Stand-ins shared by the tests."""
import time
from datetime import datetime, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

from mower_sdk.models import DeviceStateMessage, DeviceStatus
from mower_sdk.navimow_client import MowerState

RECEIVED_AT = datetime(2026, 9, 17, 20, 0, tzinfo=timezone.utc)

# The full field set the REST status endpoint has been seen to return.
REST_PAYLOAD = {
    "id": "dev-1",
    "capacityRemaining": [{"unit": "PERCENTAGE", "rawValue": 100}],
    "vehicleState": "isDocked",
    "descriptiveCapacityRemaining": "FULL",
}


class FakeMqtt:
    """The parts of the SDK's NavimowMQTT that CollectorHealth reads: the
    counters, reasons and client id, and the per-device message times, all
    set by the test."""

    def __init__(self) -> None:
        self.connects = 0
        self.disconnects = 0
        self.connect_failures = 0
        self.rebuilds = 0
        self.last_rebuild_reason: str | None = None
        self.client_id = "web_user_1"
        self.last_disconnect_reason: str | None = None
        self.last_connect_fail_reason: str | None = None
        self.message_at: dict[str, datetime] = {}
        self.location_age: dict[str, float] = {}

    def last_message_at(self, device_id, channel=None):
        assert channel is None
        return self.message_at.get(device_id)

    def last_message_age(self, device_id, channel=None):
        assert channel == "location"
        return self.location_age.get(device_id)


class FakeClient:
    """The parts of the SDK's NavimowClient the integration calls: the
    callback registrations (recorded), the restore, the command, and the
    MQTT client underneath (a FakeMqtt, or None before the connect)."""

    def __init__(self, mqtt=None) -> None:
        self.mqtt = mqtt
        self.callbacks: dict[str, list] = {
            "state": [], "event": [], "attributes": [], "rejected": [],
            "connection": [], "error": [], "rebuild_request": [],
        }
        self.restore_location = Mock()
        self.async_send_command = AsyncMock()
        self.async_disconnect = AsyncMock()
        self.last_receipt = Mock(return_value=None)

    def _register(self, channel, callback, device_id=None):
        self.callbacks[channel].append((callback, device_id))

    def on_state(self, callback, *, device_id=None):
        self._register("state", callback, device_id)

    def on_event(self, callback, *, device_id=None):
        self._register("event", callback, device_id)

    def on_attributes(self, callback, *, device_id=None):
        self._register("attributes", callback, device_id)

    def on_rejected(self, callback, *, device_id=None):
        self._register("rejected", callback, device_id)

    def on_connection(self, callback):
        self._register("connection", callback)

    def on_error(self, callback):
        self._register("error", callback)

    def on_rebuild_request(self, callback):
        self._register("rebuild_request", callback)

    def deliver(self, channel, message):
        """Call the callbacks of a channel whose device filter admits the message."""
        for callback, device_id in self.callbacks[channel]:
            if device_id is None or getattr(message, "device_id", None) == device_id:
                callback(message)


def state_message(
    state="mowing", battery=80, timestamp=1700000000, raw="isRunning",
    device_id="dev-1", received_at=RECEIVED_AT,
):
    """A state message as the SDK applies it: stamped with its receipt."""
    return DeviceStateMessage(
        device_id=device_id, timestamp=timestamp, state=state, battery=battery,
        metrics={"raw_state": raw}, received_at=received_at,
    )


def mqtt_state(location=None, **message_fields):
    """A MowerState built from a state message (source MQTT)."""
    return MowerState.from_state_message(
        state_message(**message_fields), location=location, received_monotonic=time.monotonic()
    )


def rest_state(payload=None, location=None, received_at=RECEIVED_AT):
    """A MowerState built from a REST status entry (source REST)."""
    status = DeviceStatus.from_dict(dict(payload or REST_PAYLOAD))
    return MowerState.from_status(
        status, location=location, received_at=received_at, received_monotonic=time.monotonic()
    )


def device(device_id="dev-1", name="Mower"):
    return SimpleNamespace(
        id=device_id, name=name, model="X430", firmware_version="1.0", serial_number="SN1"
    )
